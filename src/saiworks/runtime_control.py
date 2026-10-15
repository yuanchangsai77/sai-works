from __future__ import annotations


class TaskControl:
    """Identity-only task control capability issued by the trusted local Runtime owner.

    Permissions ("cancel", "approve") and the actor label are fixed at grant
    time and held by the Runtime, never by the handle. Observation handles
    (TaskAccess) must not be usable for control, and vice versa.
    """

    __slots__ = ()
