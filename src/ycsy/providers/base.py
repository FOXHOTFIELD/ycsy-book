"""Provider interface.

A provider is one platform's API. Adding support for another school's system
means implementing this protocol in a new module; nothing above it changes.
"""

from __future__ import annotations

from typing import List, Protocol, Tuple

from ..model import Appointment, Desk, Experiment


class Provider(Protocol):
    def login(self) -> str:
        """Authenticate and return a token."""

    def me(self) -> dict:
        """Current account profile."""

    def experiment(self, experiment_id: int) -> Experiment:
        """One experiment, including its lab's opening hours."""

    def experiments(self) -> List[Experiment]:
        """All experiments the account can book."""

    def desks(self, experiment: Experiment, slot: str) -> List[Desk]:
        """Desk availability for one experiment at one slot."""

    def book(self, experiment: Experiment, desk: Desk, slot: str) -> None:
        """Create an appointment."""

    def appointments(self, experiment: Experiment) -> List[Appointment]:
        """Active appointments for one experiment (cancelled ones excluded)."""

    def cancel(self, appointment_id: int) -> None:
        """Cancel one appointment."""

    def remote_link(self, desk_id: int) -> Tuple[str, dict]:
        """Remote-desktop URL plus the raw desk record."""
