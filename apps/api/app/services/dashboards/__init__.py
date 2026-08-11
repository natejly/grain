"""Dashboards: definitions the agent authors and a home screen the user arranges.

Three ideas, in the order they depend on each other:

``binding``  the contract. A template declares the dataset shape it requires and
             a binding that does not satisfy it is refused there, with every
             reason, in the same report shape the workflow compiler uses.
``store``    the writes, shared verbatim by the HTTP routes and the agent tools
             so both are held to one definition of valid.
``tools``    the registry entries that make any of it reachable — before them,
             nothing in the product could create a dashboard.
"""
from __future__ import annotations

from .binding import DashboardBindError, bind_report, rebind_spec, template_report
from .store import DashboardNameTaken

__all__ = [
    "DashboardBindError",
    "DashboardNameTaken",
    "bind_report",
    "rebind_spec",
    "template_report",
]
