# txmicro

Small web framework based on twisted.

To run:

```console
mkdir tools templates static var
virtualenv venv
. venv/bin/activate
pip install twisted mako

twistd -noy txmicro.tac
```

Point the browser to http://localhost:8000/
