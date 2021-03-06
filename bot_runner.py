import json, os.path, pprint, queue, random, subprocess, sys, threading, time
import requests

BOOK_FILE = "book.json"
CONFIG_FILE = "config.json"

pp = pprint.PrettyPrinter(indent = 4)

bot = None
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

		# Don't send uppercase True and False when doing UCI setoption...

		if msg.startswith("setoption"):
			if msg.endswith("value True"):
				msg = msg[:-4] + "true"
			if msg.endswith("value False"):
				msg = msg[:-5] + "false"

		b = bytes(msg + "\n", encoding = "ascii")
		self.process.stdin.write(b)
		self.process.stdin.flush()
		if not (msg.startswith("position") or msg.startswith("go")):
			log(self.shortname + " <-- " + msg)

# ---------------------------------------------------------------------------------------------------------------------------------

def engine_stdout_watcher(engine):

	while 1:
		msg = engine.process.stdout.readline().decode("utf-8")
		if msg == "":
			return		# EOF
		msg = msg.strip()
		engine.output.put(msg)
		if not (msg.startswith("info depth") or msg.startswith("bestmove")):
			log(engine.shortname + " --> " + msg)

def engine_stderr_watcher(engine):

	while 1:
		msg = engine.process.stderr.readline().decode("utf-8")
		if msg == "":
			return		# EOF
		msg = msg.strip()
		log(engine.shortname + " (e) " + msg)

def log(msg):

	if isinstance(msg, str):
		if msg.rstrip():
			print(msg.rstrip())
	elif isinstance(msg, dict):
		pp.pprint(msg)
	else:
		try:
			print(repr(msg))
		except:
			print("log() got unprintable msg")

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

	config.setdefault("options", {})
	config.setdefault("node_count", None)
	config.setdefault("min_tc_secs", 0)
	config.setdefault("max_tc_secs", 600)
	config.setdefault("min_inc_secs", 0)
	config.setdefault("max_inc_secs", 10)
	config.setdefault("variants", ["standard"])
	config.setdefault("blacklist", [])
	config.setdefault("whitelist", [])
	config.setdefault("allow_bots", True)
	config.setdefault("open", True)

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

	global bot

	bot = Engine(config["command"], "BOT")

	bot.send("uci")

	for key in config["options"]:
		bot.send("setoption name {} value {}".format(key, config["options"][key]))

	bot.send("ucinewgame")	# Causes Leela to actually load the network, which is good if a ultrabullet game starts.

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

	global active_game
	global active_game_MUTEX

	try:

		load_configs()		# For live adjustments

		log("Incoming challenge from {} (rated: {})".format(challenge["challenger"]["name"], challenge["rated"]))

		accepting = True

		# Already playing...

		with active_game_MUTEX:
			if active_game:
				log("But I'm in a game!")
				accepting = False

		# Not open...

		if not config["open"]:
			log("But I'm not open to challenges!")
			accepting = False

		# Variants...

		if challenge["variant"]["key"] not in config["variants"]:
			log("But it's an unacceptable variant! ({})".format(challenge["variant"]["key"]))
			accepting = False

		# Not whitelisted...

		if isinstance(config["whitelist"], list) and len(config["whitelist"]) > 0 and challenge["challenger"]["name"] not in config["whitelist"]:
			log("But challenger is not whitelisted!")
			accepting = False

		# Blacklisted...

		if isinstance(config["blacklist"], list) and challenge["challenger"]["name"] in config["blacklist"]:
			log("But challenger is blacklisted!")
			accepting = False

		# Bot...

		if challenge["challenger"]["title"] == "BOT" and not config["allow_bots"]:
			log("But I'm not accepting bots!")
			accepting = False

		# Time control...

		if challenge["timeControl"]["type"] != "clock":
			log("But it's lacking a time control!")
			accepting = False
		elif challenge["timeControl"]["limit"] < config["min_tc_secs"] or challenge["timeControl"]["limit"] > config["max_tc_secs"]:
			log("But I don't like the time control! ({}+{})".format(challenge["timeControl"]["limit"], challenge["timeControl"]["increment"]))
			accepting = False
		elif challenge["timeControl"]["increment"] < config["min_inc_secs"] or challenge["timeControl"]["increment"] > config["max_inc_secs"]:
			log("But I don't like the time control! ({}+{})".format(challenge["timeControl"]["limit"], challenge["timeControl"]["increment"]))
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

	load_configs()		# For live adjustments

	announce_start(gameId)

	threading.Thread(target = runner, args = (gameId, ), daemon = True).start()

def announce_start(gameId):

	name = os.path.basename(config["command"])

	try:
		weights = os.path.basename(config["options"]["WeightsFile"])
	except:
		try:
			weights = os.path.basename(config["options"]["EvalFile"])
		except:
			weights = "(unknown net)"

	if isinstance(config["node_count"], int) and config["node_count"] > 0:
		msg = "engine: {}, network: {}, running at {} node{}.".format(name, weights, config["node_count"], "s" if config["node_count"] > 1 else "")
	else:
		msg = "engine: {}, network: {}, running with time manager.".format(name, weights)

	log(msg)

	threading.Thread(target = delayed_tell_all, args = (gameId, msg), daemon = True).start()

# ---------------------------------------------------------------------------------------------------------

def runner(gameId):

	# So this will be its own thread, and handles the core game logic.

	global active_game
	global active_game_MUTEX

	bot.send("ucinewgame")

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
				bot.send("setoption name UCI_Chess960 value true")
			else:
				bot.send("setoption name UCI_Chess960 value false")

			try:
				if j["white"]["name"].lower() == config["account"].lower():
					colour = "white"
			except:
				pass

			try:
				if j["black"]["name"].lower() == config["account"].lower():
					colour = "black"
			except:
				pass

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
		return

	moves = []

	if state["moves"]:
		moves = state["moves"].split()

	if len(moves) % 2 == 0 and colour == "black":
		return
	if len(moves) % 2 == 1 and colour == "white":
		return

	if len(moves) > 0:
		log("           {}".format(moves[-1]))

	mymove = genmove(gameFull["initialFen"], state["moves"], state["wtime"], state["btime"], state["winc"], state["binc"])

	simple_post("https://lichess.org/api/bot/game/{}/move/{}".format(gameId, mymove))

def genmove(initial_fen, moves_string, wtime, btime, winc, binc):

	if initial_fen == "startpos":
		mv = book_move(moves_string)
		if mv:
			return mv

	if initial_fen == "startpos":
		pos_string = "startpos"
	else:
		pos_string = "fen " + initial_fen

	bot.send("position {} moves {}".format(pos_string, moves_string))

	if isinstance(config["node_count"], int) and config["node_count"] > 0:
		bot.send("go nodes {}".format(config["node_count"]))
	else:
		bot.send("go wtime {} btime {} winc {} binc {}".format(wtime, btime, winc, binc))

	bot_score = None
	bot_move = None

	while bot_move is None:

		# Read all available info...

		try:
			while 1:
				msg = bot.output.get(block = False)
				tokens = msg.split()

				if "score cp" in msg and "lowerbound" not in msg and "upperbound" not in msg:
					score_index = tokens.index("cp") + 1
					bot_score = int(tokens[score_index])
				elif "score mate" in msg:
					mate_index = tokens.index("mate") + 1
					mate_in = int(tokens[mate_index])
					if mate_in > 0:
						bot_score = 1000000 - (mate_in * 1000)
					else:
						bot_score = -1000000 + (-mate_in * 1000)
				elif "bestmove" in msg:
					bot_move = tokens[1]
					break

		except queue.Empty:
			time.sleep(0.01)

	log("      Bot: {} ({})".format(bot_move, bot_score))
	return bot_move

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
