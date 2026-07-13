"""Generated protobuf code (committed, see scripts/gen-proto.sh). Do not hand-edit.

Hand-maintained (not overwritten by `protoc --python_out`/`--pyi_out`); keep
this file even when regenerating the _pb2.py/.pyi siblings in this directory.
"""

import sys
from pathlib import Path

# protoc's python codegen emits flat, top-level imports between sibling
# _pb2 modules (e.g. live_pb2.py does `import common_pb2`) regardless of
# where the files are packaged. Put this directory on sys.path so those
# imports resolve when the package is loaded as exo.bus.gen.
_gen_dir = str(Path(__file__).parent)
if _gen_dir not in sys.path:
    sys.path.insert(0, _gen_dir)
