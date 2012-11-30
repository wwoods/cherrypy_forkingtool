
import atexit
import cherrypy
import cherrypy.process.plugins
import cherrypy.process.servers
import cherrypy.wsgiserver
import multiprocessing
import os
import signal
import socket
import threading
import time

def forkingTool():
    """Configures the CherryPy default HTTPServer for forking, and spawns
    cherrypy.config.get('server.fork_pool') forks.  Typically called immediately
    before cherrypy.engine.start().  Must be called after cherrypy.config is
    updated.
    
    Optional config:
        server.fork_life_mins - Number of minutes for a given fork to be active
                before it spawns a replacement.  Note that only processes that
                willingly exit will be restarted - kill will not let the 
                process come back to life.
                
    Using any fork_life options disable the auto reloader packaged with 
    cherrypy.  They will also prevent this method from ever returning True,
    as there is no "main fork".  The calling process will be held onto as a
    template for future forks.

    Without fork_life, registers an atexit handler in the main fork for 
    killing the child forks.

    Returns True for the main fork, and False for other forks
    """
    valid = [ 'server.fork_pool', 'server.fork_life_mins' ]
    for k in cherrypy.config.keys():
        if k.startswith('server.fork_') and k not in valid:
            raise ValueError("Unrecognized fork parameter: " + str(k))
    numForks = cherrypy.config.get('server.fork_pool', 0)
    forkLifeMins = cherrypy.config.get('server.fork_life_mins', 0)
    
    useForkLife = (forkLifeMins != 0)

    if not numForks:
        # No forking necessary!
        return True

    if threading.activeCount() != 1:
        raise Exception("Cannot fork once threads have been spawned!  {0}"
                .format(threading.enumerate()))
    
    forkList = []
    isMain = True

    # Create our shared listening socket by going through a similar code path
    # to what CherryPy would natively do.  
    # Change socket.settimeout (which is called after HTTPServer.bind()) to 
    # raise an exception class that we can pick up.
    class FakeException(Exception):
        pass
    def raiseFake(*args):
        raise FakeException()
    oldtimeout = socket.socket.settimeout
    socket.socket.settimeout = raiseFake
    try:
        cherrypy.log("Using bind_addr: " + str(cherrypy.server.bind_addr))
        hs = cherrypy.wsgiserver.HTTPServer(
            bind_addr=cherrypy.server.bind_addr
            , gateway=cherrypy.tree
            # Other parms aren't used, ignore them
        )
        hs.start()
        raise Exception("Failed to intercept HTTPServer.start() method")
    except FakeException:
        # Everything is according to plan, grab the socket and be on our way.
        s = hs.socket
        pass
    finally:
        # Restore the old settimeout for good practice
        socket.socket.settimeout = oldtimeout

    # Sanity check to make sure the HTTPServer.start() didn't cause issues
    if threading.activeCount() != 1:
        raise Exception("Cannot fork once threads have been spawned!")
    
    # Set up the socket like in cherrypy.wsgiserver.HTTPServer.bind(), and
    # disable the configuration methods used
    s.settimeout(1)
    s.listen(5) # Max pending connections

    # Override the routine that the HTTPServer usually uses to get a listening
    # socket with our own
    def newBind(self, family, type, proto=0):
        """Return our cached socket"""
        self.socket = s
    cherrypy.wsgiserver.HTTPServer.bind = newBind

    # Override the start() method to disable the socket.listen and
    # socket.settimeout
    oldStart = cherrypy.wsgiserver.HTTPServer.start
    def newStart(*args, **kwargs):
        oldListen = socket.socket.listen
        oldTimeout = socket.socket.settimeout
        socket.socket.listen = lambda *x: 0
        socket.socket.settimeout = lambda *x: 0
        try:
            return oldStart(*args, **kwargs)
        finally:
            socket.socket.listen = oldListen
            socket.socket.settimeout = oldTimeout
    cherrypy.wsgiserver.HTTPServer.start = newStart

    # Also override waiting for a free socket, since we have one
    cherrypy.process.servers.wait_for_free_port = lambda *x: 0
    
    # Do we need to set up a lifeline?
    if useForkLife:
        forksToSpawn = multiprocessing.Queue()
        # The main thread stays behind, and is not a true webserver.  It just
        # holds the socket open and keeps a "template" for new forks.  So, 
        # spawn an extra fork.
        numForks += 1

    # ..And fork our processes!
    for i in range(numForks):
        pid = os.fork()
        if pid == 0:
            # Child
            isMain = False
            cherrypy.log("Fork started")
            break
        forkList.append(pid)
        
    if isMain:
        if not useForkLife:
            # Final setup, configure our atexit stuff
            atexit.register(_killForks, forkList)
            # For the autoreloader, also kill forks on engine stop
            cherrypy.engine.subscribe('stop', lambda: _killForks(forkList))
        else:
            _forkLifeMain(forkList, forksToSpawn)
            # When we exit _forkLifeMain, we will be a new fork.
            isMain = False
            # Make sure we do NOT use the half-life trick to stagger 
            # lifetimes
            forkList = []
            
    if not isMain:
        _forkInit()
        
        # Check for our life every 1/10th of our life
        if forkLifeMins > 0:
            endTime = None
            if forkLifeMins > 0:
                endTime = time.time() + forkLifeMins * 60.0
                # Always start half the forks on an opposite period, just so 
                # that they don't all restart at once
                if len(forkList) % 2 == 1:
                    endTime -= forkLifeMins * 30.0
            _forkInit.checker = _ForkLifeChecker(forksToSpawn,
                    endTime = endTime)

    return isMain


class _ForkLifeChecker(cherrypy.process.plugins.Monitor):
    def __init__(self, addForkQueue, endTime):
        self.addForkQueue = addForkQueue
        self.endTime = endTime
        checkInterval = 60
        if endTime is not None:
            checkInterval = min(checkInterval, endTime - time.time())
        super(_ForkLifeChecker, self).__init__(cherrypy.engine, self.checkLife,
                checkInterval, name = "CherryPy Forking Tool _ForkLifeChecker")
        
        self.hasDied = False
        self.subscribe()
        
        
    def checkLife(self):
        shouldDie = False
        if self.endTime is not None and time.time() >= self.endTime:
            shouldDie = True
            
        if shouldDie and not self.hasDied:
            self.hasDied = True
            self.addForkQueue.put('fork')
            cherrypy.engine.exit()
            
            
def _checkAlive(pid):
    """Returns True if pid is alive; False otherwise.  Needed because we use
    SIG_IGN to ignore child deaths.
    """
    try:
        os.kill(pid, 0)
        return True
    except OSError, e:
        if e.errno != 3:
            # 3 -- No such process
            raise
    return False


def _forkInit():
    """Initializes this process as a fork; called before returning from
    cherrypyForkingTool."""
    # Disable autoreloader for forks
    cherrypy.config.update({ 'engine.autoreload_on': False })
    # Also remove any residual atexit handlers
    for handler in atexit._exithandlers[:]:
        method = handler[0]
        if method.__module__ not in [ 'logging' ]:
            atexit._exithandlers.remove(handler)
            
            
def _forkLifeMain(forkList, addForkQueue):
    """We're the golden model of a fork - just sit until we get permission to
    spawn a new fork, at which point return.
    """
    try:
        def onKillSignal(sig, frame):
            # As the main fork, we do not reap cherrypy's SIGTERM processing.
            # We need to convert SIGTERM into an exception so that we 
            # appropriately kill our forks and shutdown.
            raise Exception("SIGTERM received")
        signal.signal(signal.SIGTERM, onKillSignal)
        
        # We don't care about child processes.
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        
        while True:
            addForkQueue.get()
            pid = os.fork()
            if pid == 0:
                # We're the new child!  Hooray!  Unset our signal handler as
                # cherrypy will install its own.
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                return
            forkList.append(pid)
            
            # Clean out forkList
            for pid in forkList[:]:
                if not _checkAlive(pid):
                    forkList.remove(pid)
            
    except:
        # If there was any error, kill all forks and exit
        _killForks(forkList)
        raise
            
            
def _killForks(forkList):
    """For the main fork, we want to kill all children when the main fork
    dies.  This lets the forking tool work with autoreloader, for instance.
    """
    for pid in forkList:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError, e:
            # 3 means No such process
            if e.errno != 3:
                raise
    # Wait on our children to actually die, so that we don't look like we've
    # exited but are still using the port (matters for people using pidfiles).
    for pid in forkList:
        while _checkAlive(pid):
            time.sleep(0.1)
