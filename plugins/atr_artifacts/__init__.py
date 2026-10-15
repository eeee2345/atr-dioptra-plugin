"""Dioptra artifact tasks for the ATR scanner plugin.

Kept as its own plugin (mirroring the ``artifacts`` plugin in
``usnistgov/dioptra/extra``) because Dioptra loads artifact plugins into a
separate serialization context: this package must not import anything from
``atr_scanner``. It only knows how to store and load the hits table.
"""
