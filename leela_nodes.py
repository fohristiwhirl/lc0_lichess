import json, os.path, queue, random, subprocess, sys, threading, time
import requests

BOOK_FILE = "book.json"
CONFIG_FILE = "config.json"

lz = None
book = None
config = None
headers = None

active_game = None
active_game_MUTEX = threading.Lock()

class Engine():

	def __init__(self, command, shortname):

		self.shortname = shortname
		self.process = subprocess.Popen(command, shell = False, stdin = subprocess.PIPE, stdout = subprocess.PIPE, stderr = subprocess.PIPE)
		self.output = queue.Queue()

		threading.Thread(target = engine_stdout_watcher, args = (self,), daemon = True).start()
		threading.Thread(target = engine_stderr_watcher, args = (self,), daemon = True).start()

	def send(self, msg):

		msg = msg.strip()
		b = bytes(msg + "\n", encoding = "ascii")
		self.process.stdin.write(b)
		self.process.stdin.flush()
		# log(self.shortname + " <-- " + msg)

# ---------------------------------------------------------------------------------------------------------------------------------

def engine_stdout_watcher(engine):

	while 1:
		msg = engine.process.stdout.readline().decode("utf-8")
		if msg == "":
			return		# EOF
		msg = msg.strip()
		engine.output.put(msg)
		# log(engine.shortname + " --> " + msg)

def engine_stderr_watcher(engine):

	while 1:
		msg = engine.process.stderr.readline().decode("utf-8")
		if msg == "":
			return		# EOF
		msg = msg.strip()
		# log(engine.shortname + " (e) " + msg)

def log(msg):

	if isinstance(msg, str):
		if msg.rstrip():
			print(msg.rstrip())
	else:
		try:
			print(repr(msg))
		except:
			print("log() got unprintable msg...")

def simple_post(url):

	r = requests.post(url, headers = headers)

	if r.status_code != 200:
		log("Upon contacting {}:".format(url))
		try:
			log(r.json())
		except:
			log("API returned {}".format(r.status_code))

def delayed_tell_all(gameId, msg):

	# To be called in a thread, only!

	time.sleep(5)

	data1 = {"room": "player", "text": msg}
	data2 = {"room": "spectator", "text": msg}

	# Post is in x-www-form-urlencoded, which requests does by default

	for data in [data1, data2]:

		r = requests.post("https://lichess.org/api/bot/game/{}/chat".format(gameId), data = data, headers = headers)

		if r.status_code != 200:
			log("Upon contacting {}:".format(url))
			try:
				log(r.json())
			except:
				log("API returned {}".format(r.status_code))

def load_json(filename):

	with open(filename) as infile:
		ret = json.load(infile)
		return ret

def load_configs():

	global book
	global config
	global headers

	try:
		book = load_json(BOOK_FILE)
	except FileNotFoundError:
		print("Couldn't load {}".format(BOOK_FILE))
		book = []
	except json.decoder.JSONDecodeError:
		print("{} seems to be illegal JSON".format(BOOK_FILE))
		book = []

	try:
		config = load_json(CONFIG_FILE)
	except FileNotFoundError:
		print("Couldn't load {}".format(CONFIG_FILE))
		sys.exit()
		
	except json.decoder.JSONDecodeError:
		print("{} seems to be illegal JSON".format(CONFIG_FILE))
		sys.exit()

	headers = {"Authorization": "Bearer {}".format(config["token"])}

def main():

	load_configs()

	threading.Thread(target = app, daemon = True).start()

	while 1:
		try:
			time.sleep(0.5)
		except:
			print("Main thread interrupted.")
			sys.exit()	# i.e. happens if keyboard interrupt

def app():

	global lz

	lz = Engine(config["leela_command"], "LZ")

	lz.send("uci")

	for key in config["leela_options"]:
		lz.send("setoption name {} value {}".format(key, config["leela_options"][key]))

	event_stream = requests.get("https://lichess.org/api/stream/event", headers = headers, stream = True)

	for line in event_stream.iter_lines():
		if line:
			dec = line.decode("utf-8")
			j = json.loads(dec)
			if j["type"] == "challenge":
				handle_challenge(j["challenge"])
			if j["type"] == "gameStart":
				start_game(j["game"]["id"])

def handle_challenge(challenge):

	global config
	global active_game
	global active_game_MUTEX

	try:

		# Reload the config for live adjustments to whitelist...
		try:
			config = load_json(CONFIG_FILE)
		except:
			log("Reloading {} failed!".format(CONFIG_FILE))

		log("Incoming challenge from {} (rated: {}, TC: {})".format(
			challenge["challenger"]["name"],
			challenge["rated"],
			challenge["timeControl"]["show"]))

		accepting = True

		# Already playing...

		with active_game_MUTEX:
			if active_game:
				log("But I'm in a game!")
				accepting = False

		# Variants...

		if challenge["variant"]["key"] != "standard" and challenge["variant"]["key"] != "chess960":
			log("But it's a variant!")
			accepting = False

		# Not whitelisted...

		try:
			if len(config["whitelist"]) > 0 and challenge["challenger"]["name"] not in config["whitelist"]:
				log("But challenger is not whitelisted!")
				accepting = False
		except:
			pass			# No whitelist exists

		# Time control...

		if challenge["timeControl"]["type"] != "clock":
			log("But it's lacking a time control!")
			accepting = False
		elif challenge["timeControl"]["limit"] < 60 or challenge["timeControl"]["limit"] > 300:
			log("But I don't like the time control!")
			accepting = False
		elif challenge["timeControl"]["increment"] < 1 or challenge["timeControl"]["increment"] > 10:
			log("But I don't like the time control!")
			accepting = False

		if accepting:
			accept(challenge["id"])
		else:
			decline(challenge["id"])

	except Exception as err:
		log("Exception in handle_challenge(): {}".format(repr(err)))
		decline(challenge["id"])

def decline(challengeId):

	log("Declining challenge {}".format(challengeId))
	simple_post("https://lichess.org/api/challenge/{}/decline".format(challengeId))

def accept(challengeId):

	log("Accepting challenge {}".format(challengeId))
	simple_post("https://lichess.org/api/challenge/{}/accept".format(challengeId))

def abort_game(gameId):

	global active_game
	global active_game_MUTEX

	log("Aborting game {}".format(gameId))
	simple_post("https://lichess.org/api/bot/game/{}/abort".format(gameId))

	with active_game_MUTEX:
		if active_game == gameId:
			active_game = None

def start_game(gameId):

	global active_game
	global active_game_MUTEX

	autoabort = False

	with active_game_MUTEX:
		if active_game:
			autoabort = True
		else:
			active_game = gameId

	if autoabort:	# Don't do this inside the above "with", as abort() also uses the mutex.
		log("WARNING: game starting but I seem to be in a game")
		abort_game(gameId)
		return

	announce_start(gameId)

	threading.Thread(target = runner, args = (gameId, ), daemon = True).start()

def announce_start(gameId):

	try:
		weights = os.path.basename(config["leela_options"]["WeightsFile"])
	except:
		weights = "(unknown net)"

	msg = "Game {} starting. Will run {} at {} nodes.".format(gameId, weights, config["node_count"])

	log(msg)

	threading.Thread(target = delayed_tell_all, args = (gameId, msg), daemon = True).start()

# ---------------------------------------------------------------------------------------------------------

def runner(gameId):

	# So this will be its own thread, and handles the core game logic.

	global active_game
	global active_game_MUTEX

	lz.send("ucinewgame")

	events = requests.get("https://lichess.org/api/bot/game/stream/{}".format(gameId), headers = headers, stream = True)

	gameFull = None
	colour = None

	for line in events.iter_lines():

		if not line:					# Filter out keep-alive newlines
			continue

		# Each line is a JSON object containing a type field. Possible values are:
		#		gameFull	-- Full game data. All values are immutable, except for the state field.
		#		gameState	-- Current state of the game. Immutable values not included.
		#		chatLine 	-- Chat message sent by a user (or the bot itself) in the room "player" or "spectator".

		dec = line.decode("utf-8")
		j = json.loads(dec)

		if j["type"] == "gameFull":		# This should be the first thing we get.

			gameFull = j

			if j["variant"]["key"] == "chess960":
				log("setoption name UCI_Chess960 value true")
				lz.send("setoption name UCI_Chess960 value true")
			else:
				log("setoption name UCI_Chess960 value false")
				lz.send("setoption name UCI_Chess960 value false")

			if j["white"]["name"].lower() == config["account"].lower():
				colour = "white"

			if j["black"]["name"].lower() == config["account"].lower():
				colour = "black"

			handle_state(j["state"], gameId, gameFull, colour)

		elif j["type"] == "gameState":
			
			handle_state(j, gameId, gameFull, colour)

	log("Game stream closed...")

	with active_game_MUTEX:
		active_game = None

def handle_state(state, gameId, gameFull, colour):

	if state["status"] != "started":
		return

	if gameFull is None or colour is None:
		log("ERROR: handle_state() called without full info available")
		abort(gameId)

	moves = []

	if state["moves"]:
		moves = state["moves"].split()

	if len(moves) % 2 == 0 and colour == "black":
		return
	if len(moves) % 2 == 1 and colour == "white":
		return

	if len(moves) > 0:
		log("           {}".format(moves[-1]))

	mymove = genmove(gameFull["initialFen"], state["moves"])

	simple_post("https://lichess.org/api/bot/game/{}/move/{}".format(gameId, mymove))

def genmove(initial_fen, moves_string):

	if initial_fen == "startpos":
		mv = book_move(moves_string)
		if mv:
			return mv

	if initial_fen == "startpos":
		pos_string = "startpos"
	else:
		pos_string = "fen " + initial_fen

	lz.send("position {} moves {}".format(pos_string, moves_string))
	lz.send("go nodes {}".format(config["node_count"]))

	lz_score = None
	lz_move = None

	while lz_move is None:

		# Read all available LZ info...

		try:
			while 1:
				msg = lz.output.get(block = False)
				tokens = msg.split()

				if "score cp" in msg and "lowerbound" not in msg and "upperbound" not in msg:
					score_index = tokens.index("cp") + 1
					lz_score = int(tokens[score_index])
				elif "score mate" in msg:
					mate_index = tokens.index("mate") + 1
					mate_in = int(tokens[mate_index])
					if mate_in > 0:
						lz_score = 1000000 - (mate_in * 1000)
					else:
						lz_score = -1000000 + (-mate_in * 1000)
				elif "bestmove" in msg:
					lz_move = tokens[1]
					break

		except queue.Empty:
			time.sleep(0.01)

	log("      Lc0: {} ({})".format(lz_move, lz_score))
	return lz_move

def book_move(moves_string):

	mslen = len(moves_string.split())

	candidate_moves = set()

	for line in book:
		if line.startswith(moves_string) and len(line) > len(moves_string):
			try:
				candidate_moves.add(line.split()[mslen])
			except:
				pass	# Some extra whitespace in the string?

	if len(candidate_moves) == 0:
		return None

	ret = random.choice(list(candidate_moves))

	alts = []
	for mv in candidate_moves:
		if mv != ret:
			alts.append(mv)

	log("     Book: {} {}".format(ret, alts))
	return ret

# ---------------------------------------------------------------------------------------------------------

main()
