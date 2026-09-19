Use `case1_integrated_variance.py` for the current strategy. It is currently set to
`DRY_RUN = False` (live execution). See [the run guide](INTEGRATED_VARIANCE.md) for configuration.

`case1_simple.py` remains here because the current strategy imports its API
wrapper, news parser, and pricing helpers. Its tests remain alongside it.

Superseded strategies, their tests, the previous manual guide, and the old alert
log are preserved in [archive/](archive/). They are historical versions.

Run the current strategy and shared-helper tests from the repository root:

```sh
python -m pytest case1/test_case1_integrated_variance.py case1/test_case1_full_chain_optimizer.py case1/test_case1_simple.py -q
```
