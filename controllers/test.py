from txm import tview, view
#from tools.mytool import testfn

@tview('/c/{arg}')
def test4(request, c):
    return 'Hello from c! this is %s!' % (c,)

@tview('/a/b/{arg}')
def test1(request, a):
    return 'Hello world from here right here %s!' % (a,)

@tview('/a/{arg}')
def test2(request, a):
    return 'so what %s!' % (a,)

@tview('/helloworld', default=True)
def helloworld(request):
    return 'Hello world!'
