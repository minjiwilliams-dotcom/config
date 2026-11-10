# cloudsync — modular runtime (not the default entry point yet)

Python 3.11+ standard-library package extracted from `controller.py`. It is
published here additively: deployed machines keep running the flat
`controller.py` and are unaffected, because this tree is **not** listed in
`manifest.json` `files[]` (nor is it a task action).

Run it from the install directory with:

    pythonw -m cloudsync

Installers copy this folder only when their source tree contains it.
