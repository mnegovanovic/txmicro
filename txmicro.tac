import os
import sys
app_home = os.path.dirname(os.path.abspath(__file__))

from twisted.application import service
from twisted.python.log import ILogObserver, FileLogObserver
from twisted.python.logfile import DailyLogFile

sys.path.insert(0, app_home)
from txm import Web


foreground = '-noy' in sys.argv
log_dir = os.path.join(app_home, 'var')

application = service.Application('txm')
if not foreground:
    logfile = DailyLogFile("web.log", log_dir)
    application.setComponent(ILogObserver, FileLogObserver(logfile).emit)

web = Web()
web.app_home = app_home
web.setServiceParent(application)

# vim:filetype=python
