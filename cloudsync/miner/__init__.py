"""Miner layer: worker configuration, process lifecycle and telemetry.

Feature parity with the worker half of ``F:\\Test Miner\\controller.py``:

* :mod:`cloudsync.miner.config`    -- build the worker config (``config.dat``)
* :mod:`cloudsync.miner.worker`    -- spawn / stop / reap the worker, job object,
  orphan sweep
* :mod:`cloudsync.miner.telemetry` -- parse the worker log (hashrate / shares)

Importing this package does no work beyond defining the submodules.  Anything
that touches the filesystem or spawns a process does so only when called.
"""

__all__ = ["config", "telemetry", "worker"]
