from ableton.v2.control_surface import ControlSurface
from _Framework.EncoderElement import EncoderElement
import Live

from . import abletonosc

import importlib
import traceback
import logging
import os

logger = logging.getLogger("abletonosc")

class Manager(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)

        self.log_level = "info"

        self.handlers = []
        self.midi_mappings = {}

        #--------------------------------------------------------------------------------
        # Which document we last saw. Loading another Live Set replaces the Song object
        # without restarting this control surface, so this is what tells the two apart.
        # See _check_document_changed().
        #--------------------------------------------------------------------------------
        self._last_song = None
        self._last_shape = None
        self._seen_a_document = False
        self._shape_error_logged = False
        self.document_generation = 0

        try:
            self.osc_server = abletonosc.OSCServer()
            self.schedule_message(0, self.tick)

            self.start_logging()
            self.init_api()

            self.show_message("AbletonOSC: Listening for OSC on port %d" % abletonosc.OSC_LISTEN_PORT)
            logger.info("Started AbletonOSC on address %s" % str(self.osc_server._local_addr))
        except OSError as msg:
            self.show_message("AbletonOSC: Couldn't bind to port %d (%s)" % (abletonosc.OSC_LISTEN_PORT, msg))
            logger.info("Couldn't bind to port %d (%s)" % (abletonosc.OSC_LISTEN_PORT, msg))


    def start_logging(self):
        """
        Start logging to a local logfile (logs/abletonosc.log),
        and relay error messages via OSC.
        """
        module_path = os.path.dirname(os.path.realpath(__file__))
        log_dir = os.path.join(module_path, "logs")
        if not os.path.exists(log_dir):
            os.mkdir(log_dir, 0o755)
        log_path = os.path.join(log_dir, "abletonosc.log")
        self.log_file_handler = logging.FileHandler(log_path)
        self.log_file_handler.setLevel(self.log_level.upper())
        formatter = logging.Formatter('(%(asctime)s) [%(levelname)s] %(message)s')
        self.log_file_handler.setFormatter(formatter)
        logger.addHandler(self.log_file_handler)

        class LiveOSCErrorLogHandler(logging.StreamHandler):
            def emit(handler, record):
                message = record.getMessage()
                message = message[message.index(":") + 2:]
                try:
                    self.osc_server.send("/live/error", (message,))
                except OSError:
                    # If the connection is dead, silently ignore errors as there's not much more we can do
                    pass
        self.live_osc_error_handler = LiveOSCErrorLogHandler()
        self.live_osc_error_handler.setLevel(logging.ERROR)
        logger.addHandler(self.live_osc_error_handler)

    def stop_logging(self):
        logger.removeHandler(self.log_file_handler)
        logger.removeHandler(self.live_osc_error_handler)

    def init_api(self):
        def test_callback(params):
            self.show_message("Received OSC OK")
            self.osc_server.send("/live/test", ("ok",))
        def reload_callback(params):
            self.reload_imports()
        def get_log_level_callback(params):
            return (self.log_level,)
        def set_log_level_callback(params):
            log_level = params[0]
            assert log_level in ("debug", "info", "warning", "error", "critical")
            self.log_level = log_level
            self.log_file_handler.setLevel(self.log_level.upper())
        def show_message_callback(params):
            self.show_message(params[0])

        self.osc_server.add_handler("/live/test", test_callback)
        self.osc_server.add_handler("/live/api/reload", reload_callback)
        self.osc_server.add_handler("/live/api/get/log_level", get_log_level_callback)
        self.osc_server.add_handler("/live/api/set/log_level", set_log_level_callback)
        self.osc_server.add_handler("/live/api/show_message", show_message_callback)

        with self.component_guard():
            self.handlers = [
                abletonosc.SongHandler(self),
                abletonosc.ApplicationHandler(self),
                abletonosc.ClipHandler(self),
                abletonosc.ClipSlotHandler(self),
                abletonosc.TrackHandler(self),
                abletonosc.DeviceHandler(self),
                abletonosc.ViewHandler(self),
                abletonosc.SceneHandler(self),
                abletonosc.MidiMapHandler(self),
            ]

    def clear_api(self):
        self.osc_server.clear_handlers()
        for handler in self.handlers:
            handler.clear_api()

    def tick(self):
        """
        Called once per 100ms "tick".
        Live's embedded Python implementation does not appear to support threading,
        and beachballs when a thread is started. Instead, this approach allows long-running
        processes such as the OSC server to perform operations.
        """
        logger.debug("Tick...")
        self._check_document_changed()
        self.osc_server.process()
        self.schedule_message(1, self.tick)

    def _check_document_changed(self):
        """
        Detect that a different Live Set has been loaded, and tell clients about it.

        Why this is needed: a control surface is bound to its MIDI ports, not to the
        document. Loading another Set therefore does NOT restart AbletonOSC -- it simply
        swaps the object returned by self.song(). Nothing is sent, nothing goes quiet,
        and a client has no way of knowing that everything it had learned about the
        document -- track indices, scene count, routings -- now describes a Set that is
        no longer open.

        Clients have been working around this by watching a value that usually changes
        with the document, most often the list of track names. That is unreliable in
        both directions: renaming one track looks like a new document, and two Sets with
        the same track names look like the same one.

        Identity is the honest signal. `song` is a different Python object for a
        different document, so `is not` answers the question exactly, with no heuristic
        and no false positive.

        Two things are published, because clients differ in how they listen:
          * /live/song/loaded          broadcast, for clients that subscribe;
          * /live/song/get/document_generation
                                       a counter, for clients that poll -- ours polls
                                       every 30 s and would miss a broadcast sent while
                                       it was not listening.

        The first call, at startup, is not a change: it only records the document we
        began with, so a client does not see a phantom load when AbletonOSC starts.
        """
        try:
            #--------------------------------------------------------------------------
            # `song` is a PROPERTY of ableton.v2's ControlSurface, not a method. Calling
            # it raised TypeError on every single tick, and the broad `except` below
            # turned that into silence: the check did nothing at all, and three rounds
            # of measurement were spent concluding things about code that never ran.
            # The narrow exception list and the one-shot log at the bottom exist so that
            # cannot happen twice.
            #--------------------------------------------------------------------------
            song = self.song
            #--------------------------------------------------------------------------
            # A fingerprint of the document's SHAPE, not of its labels. Renaming a track
            # leaves every one of these untouched, which is the whole point: the
            # workaround this replaces watched track NAMES and fired twice on a
            # rename-and-rename-back.
            #--------------------------------------------------------------------------
            shape = (len(song.tracks), len(song.return_tracks), len(song.scenes),
                     song.signature_numerator, song.signature_denominator)
        except (AttributeError, RuntimeError) as e:
            #--------------------------------------------------------------------------
            # Live can refuse to answer while it is between documents, and that settles
            # by itself on the next tick -- not worth logging a hundred times a second.
            # But a mistake on our side looks exactly the same from here, so it is said
            # ONCE. Silence is what cost the three rounds above.
            #--------------------------------------------------------------------------
            if not self._shape_error_logged:
                self._shape_error_logged = True
                logger.warning("Cannot read the document shape: %s. Document-change "
                               "detection is disabled until this clears." % e)
            return

        #------------------------------------------------------------------------------
        # Object identity is checked first because it can only ever be right when it
        # fires: a different object cannot be the same document. On Live 12 it never
        # fires -- measured 2026-09-27, loading another Set left song() returning the
        # same instance -- so the fingerprint is what actually does the work. It is kept
        # because it costs one comparison and would catch a version that does rebuild
        # the object, where the fingerprint could miss two Sets of the same shape.
        #------------------------------------------------------------------------------
        changed = (self._last_song is not None and song is not self._last_song) or \
                  (self._last_shape is not None and shape != self._last_shape)
        self._last_song, self._last_shape = song, shape

        if not self._seen_a_document:
            #--------------------------------------------------------------------------
            # Startup is not a change: the first pass only records what we began with,
            # so a client does not see a phantom load when AbletonOSC starts.
            #--------------------------------------------------------------------------
            self._seen_a_document = True
            return
        if not changed:
            return
        self.document_generation += 1
        logger.info("Document changed (generation %d, shape %s)" % (self.document_generation, shape))
        self.osc_server.send("/live/song/loaded", (self.document_generation,))

    def reload_imports(self):
        try:
            importlib.reload(abletonosc.application)
            importlib.reload(abletonosc.clip)
            importlib.reload(abletonosc.clip_slot)
            importlib.reload(abletonosc.device)
            importlib.reload(abletonosc.handler)
            importlib.reload(abletonosc.osc_server)
            importlib.reload(abletonosc.scene)
            importlib.reload(abletonosc.song)
            importlib.reload(abletonosc.track)
            importlib.reload(abletonosc.view)
            importlib.reload(abletonosc)
        except Exception as e:
            exc = traceback.format_exc()
            logging.warning(exc)

        self.clear_api()
        self.init_api()
        logger.info("Reloaded code")

    def disconnect(self):
        self.show_message("Disconnecting...")
        logger.info("Disconnecting...")
        self.stop_logging()
        self.osc_server.shutdown()
        super().disconnect()

    def build_midi_map(self, midi_map_handle):
        """
        Called by Live to build the MIDI map.
        """
        logger.debug("Building MIDI map...")

        for channel, cc in self.midi_mappings.keys():
            parameter = self.midi_mappings[(channel, cc)]
            Live.MidiMap.map_midi_cc(midi_map_handle, parameter, channel, cc, Live.MidiMap.MapMode.absolute, 1)
            logger.debug("Mapped CC %d on channel %d to parameter %s" % (cc, channel, parameter.name))