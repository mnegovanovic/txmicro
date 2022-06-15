import os
import sys
import re
import pprint
import pickle
import uuid
import inspect
import threading
import time
import json
from io import StringIO
from configparser import SafeConfigParser

from twisted.internet import reactor, defer
from twisted.application import service
from twisted.web import server, resource, http, util as webutil
from twisted.python import log, failure
from twisted.web.static import File
from twisted.internet.threads import deferToThread as dTT

from mako.lookup import TemplateLookup


#
# some constants
#
LONG_POLL = 0


#
# base decorators
#
def tview(path, method=['GET', 'POST'], template=None, default=False):
    global APP

    def _(fn):
        view_dict = {}
        view_dict['view'] = True
        view_dict['path'] = path
        view_dict['method'] = method
        if template is None:
            view_dict['template'] = None
        elif template is True:
            view_dict['template'] = '%s_%s.mak' % ('_'.join(path), fn.__name__)
        else:
            view_dict['template'] = '%s.mak' % (template,)
        view_dict['default'] = default
        view_dict['in_thread'] = True

        parts = path.split("/")
        if parts[0] == '':
            parts = parts[1:]
        ppath = []
        args = 0
        for p in parts:
            if p.startswith("{") and p.endswith("}"):
                args = args + 1
            else:
                ppath.append(p)
                if args > 0:
                    raise Exception("Invalid placement of path arguments '%s'" % path,)
        
        view_dict['fn'] = fn
        if args != len(inspect.getargspec(fn)[0]) - 1:
            raise Exception("Invalid number of path arguments '%s'" % path,)
        view_dict['args'] = args
        
        def _check(node, path):
            if not path:
                return node

            p = path[0]
            path = path[1:]

            if p not in node:
                node[p] = {}
            return _check(node[p], path)

        node = _check(APP, ppath)

        if 'view_dict' not in node:
            node['view_dict'] = {}
        for m in method:
            node['view_dict'][m] = view_dict

        if default:
            APP['_default_view'] = node

        fn._txm_view_dict = view_dict
        return fn
    return _


def view(path, method=['GET', 'POST'], template=None, default=False):
    global APP

    def _(fn):
        tview(path, method, template, default)(fn)
        fn._txm_view_dict['in_thread'] = False
        return fn
    return _


#
# Mako templates
#
TEMPLATE_PATH = None
_MAKO_LOOKUP = None


def getTemplatePath():
    global TEMPLATE_PATH
    return TEMPLATE_PATH


#
# APP
#
APP_HOME = None
APP = {}


def setAppHome(path):
    global APP_HOME
    global TEMPLATE_PATH
    global cfg

    APP_HOME = path
    cfg = CFG()

    TEMPLATE_PATH = []
    TEMPLATE_PATH.append(os.path.join(APP_HOME, 'templates'))
    log.msg('TEMPLATE PATH: ' + str(TEMPLATE_PATH))


def loadApp():
    global APP
    global APP_HOME

    # null
    APP = {}

    # construct controllers dir and insert it into python path
    cdir = os.path.join(APP_HOME, 'controllers')

    if 'controllers' in sys.modules:
        del sys.modules['controllers']
    if 'tools' in sys.modules:
        del sys.modules['tools']

    to_delete = []
    for k, v in sys.modules.items():
        if k.startswith('controllers.') or k.startswith('tools.'):
            to_delete.append(k)
    for td in to_delete:
        del sys.modules[td]

    ccandidates = os.listdir(cdir)
    for ccandidate in ccandidates:
        if ccandidate.endswith('.py') and not ccandidate == '__init__.py':
            __import__('controllers.' + ccandidate[:-3], None, None, [])


#
# App controller reloader
#
RELOAD = False


def startAppLoader(app):
    global RELOAD
    RELOAD = True
    loader = AppLoader(app)
    loader.start()


def stopAppLoader():
    global RELOAD
    RELOAD = False


class AppLoader(threading.Thread):
    def __init__(self, app):
        threading.Thread.__init__(self)
        self.app = app

    def run(self):
        global APP_HOME
        global RELOAD
        mtimes = {}
        while(RELOAD):
            modified = False
            for target_dir in ['controllers', 'tools']:
                cdir = os.path.join(APP_HOME, target_dir)
                cfiles = os.listdir(cdir)
                for cfile in cfiles:
                    if not cfile.endswith('.py'):
                        continue
                    path = os.path.join(cdir, cfile)
                    mtime = os.path.getmtime(path)
                    if not mtime == mtimes.get(cfile, 0):
                        mtimes[cfile] = mtime
                        modified = True
            if modified:
                try:
                    log.msg('RELOADING APP')
                    loadApp()
                    self.app.loading_error = None
                except:
                    reason = failure.Failure()
                    log.err(reason)
                    self.app.loading_error = webutil.formatFailure(reason)

            time.sleep(0.250)


#
# App implementation and service class
#
def notFound(request):
    return resource.ErrorPage(http.NOT_FOUND,
                              "Resource not found",
                              "Requested resource was not found on this server")


class LoadingError(resource.Resource):
    def __init__(self, body):
        resource.Resource.__init__(self)
        self.body = body

    def getChild(self, path, request):
        return self

    def render_GET(self, request):
        request.setHeader("content-type", "text/html; charset=utf-8")
        return """<html><head><title>txmicro Web Traceback (most recent call last)</title></head>
        <body><b>web.Server Traceback (most recent call last):</b>\n\n
        %s\n\n</body></html>\n""" % (self.body,)

    render_POST = render_GET


class App(resource.Resource):
    def __init__(self):
        resource.Resource.__init__(self)

        global _MAKO_LOOKUP

        self.response_encoding = cfg.get('WEB', 'RESPONSE_ENCODING', 'UTF-8')
        self.loading_error = None
        self.mako_lookup = TemplateLookup(directories=getTemplatePath(),
                                          strict_undefined=False,
                                          module_directory='/tmp/mako_%s' % (uuid.uuid4().hex,),
                                          #output_encoding=self.response_encoding,
                                          input_encoding='utf-8',
                                          #encoding_errors='replace',
                                          default_filters=['h'])
        _MAKO_LOOKUP = self.mako_lookup

    def getChild(self, path, request):
        global APP

        # if we got controller loading error
        if self.loading_error is not None:
            return LoadingError(self.loading_error)

        # bytes to string
        path = path.decode('utf-8')
        prepath = [x.decode('utf-8') for x in request.prepath]
        postpath = [x.decode('utf-8') for x in request.postpath]
        method = request.method.decode('utf8')
        print(path, prepath, postpath)

        # set up txmicro specific stuff
        if not hasattr(request, 'txm'):
            request.txm = {}
            request.txm['app'] = self
            request.txm['node'] = APP
            request.txm['response_encoding'] = self.response_encoding

            # we want to know if request connection dies prematurely
            def _success(result):
                pass

            def _fail(reason):
                request._ended = True
            d = request.notifyFinish()
            d.addCallbacks(_success, _fail)

        # finally find the view
        if len(prepath) == 1 and not path:
            if '_default_view' in APP:
                request.txm['node'] = APP['_default_view']
                if method in request.txm['node']['view_dict'] and \
                        request.txm['node']['view_dict'][method]['args'] == 0:
                    return self
                else:
                    return notFound(request)
            else:
                return notFound(request)

        if path in request.txm['node']:
            request.txm['node'] = request.txm['node'][path]
            for p in postpath:
                if p in request.txm['node']:
                    request.txm['node'] = request.txm['node'][p]
                    postpath.remove(p)
            
            if method in request.txm['node']['view_dict']:
                if request.txm['node']['view_dict'][method]['args'] == len(postpath):
                    request.txm['path_args'] = postpath
                    request.postpath = []
                    return self
                else:
                    return notFound(request)
            else:
                return notFound(request)
        else:
            return notFound(request)

    def render_GET(self, request):
        """
        Override twisted.web.resource.Resource default method.
        """
        #
        # request finish/cleanup functions
        #
        def _renderMako(view_ns):
            # make sure request has not ended already
            if request.finished:
                return
            if hasattr(request, '_ended'):
                return

            # rendering for different template engines
            #view_ns['template_js_file'] = view_dict['template_js_file']
            return mako_template.render(**view_ns)

        def _finish_up(render_result):
            if request.finished:
                request.txm = None
                return
            if hasattr(request, '_ended'):
                request.txm = None
                return
            if render_result == LONG_POLL:
                return

            if render_result is None:
                render_result = ''

            if type(render_result) is str:
                render_result = render_result.encode(self.response_encoding)

            request.setResponseCode(http.OK)
            #request.setHeader('content-type',"text/html")
            request.setHeader('content-length', str(len(render_result)))
            request.write(render_result)
            request.txm = None
            request.finish()

        #
        # render "normal" page
        #
        view_dict = request.txm['node']['view_dict'][request.method.decode('utf8')]

        args = []
        for i in range(view_dict['args']):
            try:
                args.append(request.txm['path_args'][i])
            except:
                args.append(None)

        if view_dict['in_thread']:
            d = dTT(view_dict['fn'], request, *args)
        else:
            d = defer.maybeDeferred(view_dict['fn'], request, *args)

        if view_dict['template'] is None:
            d.addCallbacks(_finish_up, request.processingFailed)
        else:
            mako_template = self.mako_lookup.get_template(view_dict['template'])
            d.addCallbacks(_renderMako)
            d.addCallbacks(_finish_up, request.processingFailed)

        return server.NOT_DONE_YET

    render_POST = render_GET

    def render_PUT(self, request):
        data = json.loads(request.content.read().decode("utf-8"))
        request._data = data

        view_dict = request.txm['node']['view_dict'][request.method.decode('utf8')]

        args = []
        for i in range(view_dict['args']):
            try:
                args.append(request.txm['path_args'][i])
            except:
                args.append(None)

        if view_dict['in_thread']:
            d = dTT(view_dict['fn'], request, *args)
        else:
            d = defer.maybeDeferred(view_dict['fn'], request, *args)

        def _finish_up(render_result):
            if request.finished:
                request.txm = None
                return
            if hasattr(request, '_ended'):
                request.txm = None
                return
            if render_result == LONG_POLL:
                return

            if render_result is None:
                render_result = ''

            if type(render_result) is str:
                render_result = render_result.encode(self.response_encoding)
            else:
                render_result = json.dumps(render_result)

            request.setResponseCode(http.OK)
            #request.setHeader('content-type',"text/html")
            request.setHeader('content-length', str(len(render_result)))
            request.write(render_result)
            request.txm = None
            request.finish()

        d.addCallbacks(_finish_up, request.processingFailed)
        return server.NOT_DONE_YET


class Web(service.Service):
    def privilegedStartService(self):
        global APP
        global APP_HOME

        # set up application object
        setAppHome(self.app_home)
        loadApp()
        app = App()
        app_str = StringIO()
        pprint.pprint(APP, app_str)
        log.msg("App: \n" + app_str.getvalue())
        if not cfg.getYN('WEB', 'IN_PRODUCTION', True):
            startAppLoader(app)
            log.msg("App: app reloader started")

        # set up static resource
        static_resource = File(os.path.join(APP_HOME, 'static'))
        app.putChild('static', static_resource)

        # set up site
        self.site = TXMSite(app)
        self.site.requestFactory = TXMRequest
        self.site.sessionFactory = txmSessionFactory
        self.loadSessions(self.site)

        # start web server
        reactor.suggestThreadPoolSize(int(cfg.get('WEB', 'THREADS', 10)))
        self.port = reactor.listenTCP(int(cfg.get('WEB', 'PORT', 8000)), self.site)

    def stopService(self):
        self.running = 0
        self.port.stopListening()
        stopAppLoader()
        self.saveSessions(self.site)

    def saveSessions(self, site):
        data = {}
        for uid, session in site.sessions.items():
            if hasattr(session, 'data'):
                data[uid] = session.data
        session_file = os.path.join(APP_HOME, 'var', 'sessions.pkl')
        try:
            pickle.dump(data, open(session_file, 'wb'))
        except:
            os.remove(session_file)

    def loadSessions(self, site):
        data = {}
        session_file = os.path.join(APP_HOME, 'var', 'sessions.pkl')
        if not os.path.exists(session_file):
            return

        # load
        try:
            data = pickle.load(open(session_file, 'rb'))
        except:
            pass
        else:
            for uid, session_data in data.items():
                session = site.sessionFactory(site, uid)
                session.data = session_data
                site.sessions[uid] = session
                session.startCheckingExpiration()
        finally:
            os.remove(session_file)


class TXMRequest(server.Request):
    def getArg(self, arg_name):
        arg = self.args.get(arg_name, None)

        if arg is None:
            return None
        try:
            return arg[0].strip()
        except:
            return None

    def getArgsMatching(self, pattern):
        matches = []
        for key in self.args.keys():
            if re.match(pattern, key):
                matches.append(key)

        result = {}
        for match in matches:
            result[match] = self.getArg(match)
        return result

    def redirect(self, url):
        server.Request.redirect(self, url)


def txmSessionFactory(*args, **kwargs):
    session = server.Session(*args, **kwargs)
    session.sessionTimeout = int(cfg.get('WEB', 'SESSION_TIMEOUT', 3600))
    return session


class TXMSite(server.Site):
    def log(self, request):
        if cfg.getYN('WEB', 'ACCESS_LOG', True):
            server.Site.log(self, request)
        pass


#
# conf/cfg from ini file
#
cfg = None
DELIM = re.compile(r",")


class CFG(object):
    def __init__(self):
        global APP_HOME
        self.parser = SafeConfigParser()
        self.parser.read(os.path.join(APP_HOME, 'cfg.ini'))

    def get(self, section, var, default=None):
        v = default
        try:
            v = self.parser.get(section, var)
            return v
        except:
            log.err("Missing var in cfg.ini '" + section + "/" + var + "' returning default.")
            return v

    def getYN(self, section, var, default=None):
        v = default
        try:
            v = self.parser.get(section, var)
            if v in ['yes', 'Yes', 'YES', 'true', 'True', 'TRUE', 'on', 'On', 'ON']:
                v = True
            elif v in ['no', 'No', 'NO', 'false', 'False', 'FALSE', 'off', 'Off', 'OFF']:
                v = False
            else:
                log.err('Invalid value in cfg.ini \'%s\', section \'%s\' var \'%s\'' % (v, section, var))
            return v
        except:
            log.err("Missing var in cfg.ini '" + section + "/" + var + "' returning default.")
            return v

    def getAll(self, section, var, default=None):
        v = default
        try:
            v = self.parser.get(section, var)
            v = DELIM.split(v)
            v = [x.strip() for x in v if x.strip()]
            return v
        except:
            log.err("Missing var in cfg.ini '" + section + "/" + var + "' returning default.")
            return v
