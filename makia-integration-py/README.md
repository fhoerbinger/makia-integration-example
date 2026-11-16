# makia-integration

Connect a customer system to MAKIA through its stable API: sync projects and
members, send records with files, read results back into your own tables. You
write a source and a sink; the package runs the loop. Python 3.12+, MIT.

Install offline from wheels:

    pip install --no-index --find-links wheels makia-integration

Start a integration:

    makia-integration new my-integration
    cd my-integration && pytest

Then read the guide: `makia-integration docs` prints the folder; open `guide.md`.
