# blaze's changes to the standard library

library/ is the Rust project's standard library at the commit of blaze's nightly
(UPSTREAM) with blaze's changes. blaze changes it freely: the record of what changed is the
diff against upstream, which `python3 harness/library.py check '{}'` lists. This file is a
log of the notable changes and why, not a list a change has to be entered in.

To change the standard library: edit it under library/, run
`python3 harness/library.py digest '{}'`, commit. The daily bump moves library/ to the next
nightly as a merge (`harness/library.py merge`), so the change is kept; a conflict stops the
bump with an issue (docs/toolchain.md, "The daily bump").

None yet.
