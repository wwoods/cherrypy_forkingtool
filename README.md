How to use
==========
The tool just needs to be imported and called right before you start the cherrypy engine; something like:

    import cherrypyForkingTool
    cherrypyForkingTool.forkingTool()
    cherrypy.engine.start()
    cherrypy.engine.block()

After that, modify the [global] section of your cherrypy's configuration, or use cherrypy.config.update() to set the following properties:

* server.fork_pool - The number of forks to spawn.  Note that the total number of server processes will be one higher than this.
* server.fork_life_mins - The maximum number of minutes that any single fork should accept requests; used as a really cheap memory leak fixer.

Read the source to learn more.


