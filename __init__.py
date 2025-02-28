import chess
from chess.variant import find_variant
import json
import logging
import multiprocessing
import traceback
import signal
import sys
import time
import backoff
from requests.exceptions import ChunkedEncodingError, ConnectionError, HTTPError
from urllib3.exceptions import ProtocolError
from .ColorLogger import enable_color_logging
from . import model, lichess, logging_pool
from .conversation import Conversation, ChatLine

logger = logging.getLogger(__name__)

try:
    from http.client import RemoteDisconnected
    # New in version 3.5: Previously, BadStatusLine('') was raised.
except ImportError:
    from http.client import BadStatusLine as RemoteDisconnected

__version__ = "1.1.4"

terminated = False

def signal_handler(signal, frame):
    global terminated
    logger.debug("Recieved SIGINT. Terminating client.")
    terminated = True

signal.signal(signal.SIGINT, signal_handler)

# Stupid hack for windows error where signal only works in main thread.
if sys.platform.startswith('win'):
    def nothing(*args, **kwargs):
        pass
    signal.signal = nothing

def is_final(exception):
    return isinstance(exception, HTTPError) and exception.response.status_code < 500

def upgrade_account(li):
    if li.upgrade_to_bot_account() is None:
        return False

    logger.info("Succesfully upgraded to Bot Account!")
    return True

@backoff.on_exception(backoff.expo, BaseException, max_time=600, giveup=is_final)
def watch_control_stream(control_queue, li):
    response = li.get_event_stream()
    try:
        for line in response.iter_lines():
            if line:
                event = json.loads(line.decode('utf-8'))
                control_queue.put_nowait(event)
            else:
                control_queue.put_nowait({"type": "ping"})
    except (RemoteDisconnected, ChunkedEncodingError, ConnectionError, ProtocolError) as exception:
        logger.error("Terminating client due to connection error")
        traceback.print_exception(type(exception), exception, exception.__traceback__)
        control_queue.put_nowait({"type": "terminated"})

def start(li, user_profile, engine_factory, config):
    challenge_config = config["challenge"]
    max_games = challenge_config.get("concurrency", 1)
    logger.info("You're now connected to {} and awaiting challenges.".format(config["url"]))
    manager = multiprocessing.Manager()
    challenge_queue = manager.list()
    control_queue = manager.Queue()
    control_stream = multiprocessing.Process(target=watch_control_stream, args=[control_queue, li])
    control_stream.start()
    busy_processes = 0
    queued_processes = 0

    with logging_pool.LoggingPool(max_games+1) as pool:
        while not terminated:
            event = control_queue.get()
            if event["type"] == "terminated":
                break
            elif event["type"] == "local_game_done":
                busy_processes -= 1
                logger.info("+++ Process Free. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
            elif event["type"] == "challenge":
                chlng = model.Challenge(event["challenge"])
                if chlng.is_supported(challenge_config):
                    challenge_queue.append(chlng)
                    if (challenge_config.get("sort_by", "best") == "best"):
                        list_c = list(challenge_queue)
                        list_c.sort(key=lambda c: -c.score())
                        challenge_queue = list_c
                else:
                    try:
                        li.decline_challenge(chlng.id)
                        logger.info("    Decline {}".format(chlng))
                    except HTTPError as exception:
                        if exception.response.status_code != 404: # ignore missing challenge
                            raise exception
            elif event["type"] == "gameStart":
                if queued_processes <= 0:
                    logger.debug("Something went wrong. Game is starting and we don't have a queued process")
                else:
                    queued_processes -= 1
                game_id = event["game"]["id"]
                pool.apply_async(play_game, [li, game_id, control_queue, engine_factory, user_profile, config, challenge_queue])
                busy_processes += 1
                logger.info("--- Process Used. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
            while ((queued_processes + busy_processes) < max_games and challenge_queue): # keep processing the queue until empty or max_games is reached
                chlng = challenge_queue.pop(0)
                try:
                    response = li.accept_challenge(chlng.id)
                    logger.info("    Accept {}".format(chlng))
                    queued_processes += 1
                    logger.info("--- Process Queue. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
                except HTTPError as exception:
                    if exception.response.status_code == 404: # ignore missing challenge
                        logger.info("    Skip missing {}".format(chlng))
                    else:
                        raise exception
    logger.info("Terminated")
    control_stream.terminate()
    control_stream.join()

@backoff.on_exception(backoff.expo, BaseException, max_time=600, giveup=is_final)
def play_game(li, game_id, control_queue, engine_factory, user_profile, config, challenge_queue):
    response = li.get_game_stream(game_id)
    lines = response.iter_lines()

    #Initial response of stream will be the full game info. Store it
    game = model.Game(json.loads(next(lines).decode('utf-8')), user_profile["username"], li.baseUrl, config.get("abort_time", 20))
    board = setup_board(game)
    engine = engine_factory()
    conversation = Conversation(game, engine, li, __version__, challenge_queue)

    logger.info("+++ {}".format(game))

    try:
        play_first_move(game, engine, board, li)

        for binary_chunk in lines:
            upd = json.loads(binary_chunk.decode('utf-8')) if binary_chunk else None
            u_type = upd["type"] if upd else "ping"
            if u_type == "chatLine":
                conversation.react(ChatLine(upd), game)
            elif u_type == "gameState":
                game.state = upd
                moves = upd["moves"].split()
                board = update_board(board, moves[-1])
                if not board.is_game_over() and is_engine_move(game, moves):
                    best_move = engine.move(board, {
                        key: upd[key] for key in upd.keys()
                            & {'wtime', 'btime', 'winc', 'binc'}
                    })
                    li.make_move(game.id, best_move)
                    game.abort_in(config.get("abort_time", 20))
            elif u_type == "ping":
                if game.should_abort_now():
                    logger.info("    Aborting {} by lack of activity".format(game.url()))
                    li.abort(game.id)
    except HTTPError as e:
        ongoing_games = li.get_ongoing_games()
        game_over = True
        for ongoing_game in ongoing_games:
            if ongoing_game["gameId"] == game.id:
                game_over = False
                break
        if not game_over:
            logger.warn("Abandoning game due to HTTP "+response.status_code)
    except (RemoteDisconnected, ChunkedEncodingError, ConnectionError, ProtocolError) as exception:
        logger.error("Abandoning game due to connection error")
        traceback.print_exception(type(exception), exception, exception.__traceback__)
    finally:
        logger.info("--- {} Game over".format(game.url()))
        engine.stop()
        # This can raise queue.NoFull, but that should only happen if we're not processing
        # events fast enough and in this case I believe the exception should be raised
        control_queue.put_nowait({"type": "local_game_done"})


def play_first_move(game, engine, board, li):
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):
        # need to hardcode first movetime since Lichess has 30 sec limit.
        best_move = engine.move(board, {'movetime': 10000,})
        li.make_move(game.id, best_move)
        return True
    return False

def setup_board(game):
    if game.variant_name.lower() == "chess960":
        board = chess.Board(game.initial_fen, chess960=True)
    elif game.variant_name == "From Position":
        board = chess.Board(game.initial_fen)
    else:
        VariantBoard = find_variant(game.variant_name)
        board = VariantBoard()
    moves = game.state["moves"].split()
    for move in moves:
        board = update_board(board, move)

    return board


def is_white_to_move(game, moves):
    return len(moves) % 2 == (0 if game.white_starts else 1)

def is_engine_move(game, moves):
    return game.is_white == is_white_to_move(game, moves)

def update_board(board, move):
    uci_move = chess.Move.from_uci(move)
    board.push(uci_move)
    return board

def intro():
    return r"""
    .   _/|
    .  // o\
    .  || ._)  lichess-bot %s
    .  //__\
    .  )___(   Play on Lichess with a bot
    """ % __version__

def serve_lichess(
        token: str,
        engine_factory,
        concurrency = 1,
        time_controls = ['bullet', 'blitz', 'rapid'],
        modes = ['casual'],
        verbose = False,
        upgrade = False,
        url = 'https://lichess.org/'):
    """
    Serve an engine to lichess.

    Args
    token: Your lichess OAUTH2 API token.
    engine_factory: A function that can be called to get a new engine handle.
    concurrency: How meny games to play in parallel
    time_controls: A list of time controls to play, valid entries are
        ultraBullet
        bullet
        blitz
        rapid
        classical
        correspondence

    modes: What modes to play, valid entires are
        casual
        rated

    verbose: Verbose logging
    upgrade: Upgrade to a bot account if needed
    url: Lichess base url
    """

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)-15s: %(message)s")
    enable_color_logging(debug_lvl=logging.DEBUG if verbose else logging.INFO)
    logger.info(intro())

    li = lichess.Lichess(token, url, __version__)

    user_profile = li.get_profile()
    username = user_profile["username"]
    is_bot = user_profile.get("title") == "BOT"
    logger.info("Welcome {}!".format(username))

    if upgrade and is_bot is False:
        is_bot = upgrade_account(li)


    # I'm lazy, I don't want to refactor all the code to stop using a CONFIG dict
    # So I'll just construct it here.
    CONFIG = {
        'url': url,
        'abort_time': 20,
        'challenge': {
            'concurrency': concurrency,
            'sort_by': 'first', # possible values: "best", "first"
            'accept_bot': True,
            'max_increment': 180,
            'min_increment': 0,
            'variants': ['standard'],
            'time_controls': time_controls,
            'modes': modes,
        },
    }

    if is_bot:
        start(li, user_profile, engine_factory, CONFIG)
    else:
        logger.error("{} is not a bot account. Please upgrade it to a bot account!".format(user_profile["username"]))
