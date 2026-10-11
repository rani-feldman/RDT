"""RDT desktop GUI (PySide6): connection bar, watch tabs, variable view, chart, memory view, SCPI
console and profile editor, all working on one workspace (``.rdtw``).

Every panel calls the core through ``Device``, which runs each device access in order on one
worker thread, so a slow or silent device never freezes the window.
"""
