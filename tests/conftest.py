"""This package's own pytest plugin is installed while its tests run, so it
would otherwise collect the manifests under fixtures/ as live simulations.
They exist to be parsed, not run — there is no project behind them.
"""

collect_ignore_glob = ["fixtures/*"]
