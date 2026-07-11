"""Abstract base class for miner API clients."""

from __future__ import annotations

import abc

from tuner_app.miner.types import BoardSummary, HardwareTopology, MinerSummary


class MinerAPI(abc.ABC):
    """Abstract base class for miner API clients.

    Defines the contract that all vendor-specific subclasses must honour.
    Concrete implementations (EpicMinerAPI, BixbitMinerAPI) override every
    abstract method.  The constructor is concrete and shared by all subclasses.
    """

    def __init__(self, ip, port=4028, password="letmein"):
        self.ip = ip
        self.port = port
        self.base = f"http://{ip}:{port}"
        self.password = password

    @abc.abstractmethod
    def summary(self) -> MinerSummary:
        """Return a typed MinerSummary DTO populated from this miner's
        summary response. Subclasses delegate to self.summary() and
        pass the raw dict through MinerSummary.from_epic() or
        MinerSummary.from_bixbit() as appropriate.
        """
        ...

    def summary_lite(self) -> MinerSummary:
        """Cheap liveness probe for poll loops — populates only the fields
        that reflect *current* miner state (operating_state, hashrate_ths).

        Default delegates to ``summary()``. Vendors whose ``summary()`` fans
        out into many TCP cmds (LuxOS fires 10) override this with a
        single-cmd path. Callers in recovery / wait loops should use this
        instead of ``summary()`` to avoid storming the firmware. Other
        fields on the returned DTO may be None / 0 — do not read them.
        """
        return self.summary()

    @abc.abstractmethod
    def clocks(self) -> list[BoardSummary]:
        """Return a list of BoardSummary populated from this miner's
        clocks response. Each BoardSummary has `index` and
        `chip_freqs_mhz` populated; other BoardSummary fields stay at
        defaults (None / []).

        On vendors without per-chip clock API (e.g., Bixbit), returns [].
        """
        ...

    @abc.abstractmethod
    def temps(self) -> list[BoardSummary]:
        """Return a list of BoardSummary from this miner's temps response.

        Each BoardSummary has index, temp_inlet_c, temp_outlet_c populated;
        other fields stay at defaults. On vendors without board temp API, returns [].
        """
        ...

    @abc.abstractmethod
    def temps_chip(self) -> list[BoardSummary]:
        """Return a list of BoardSummary from this miner's temps_chip response.

        Each BoardSummary has index and chip_temps_c populated; other fields
        stay at defaults. On vendors without per-chip temp API, returns [].
        """
        ...

    @abc.abstractmethod
    def hashrate(self) -> list[BoardSummary]:
        """Return a list of BoardSummary from this miner's hashrate response.

        Each BoardSummary has index, health_pct, and hashrate_per_chip_mhs
        populated; other fields stay at defaults. On vendors without per-chip
        hashrate API, returns [].
        """
        ...

    @abc.abstractmethod
    def capabilities(self):
        pass

    @abc.abstractmethod
    def voltages(self):
        pass

    @abc.abstractmethod
    def set_voltage(self, mv):
        pass

    @abc.abstractmethod
    def set_clock_all(self, mhz):
        pass

    @abc.abstractmethod
    def set_clock_board(self, board_clocks):
        pass

    @abc.abstractmethod
    def set_clock_chip(self, board_index, chip_freqs):
        pass

    @abc.abstractmethod
    def set_perpetualtune(self, enabled):
        pass

    @abc.abstractmethod
    def set_coin(self, coin, stratum_configs, unique_id=False):
        pass

    @abc.abstractmethod
    def start_mining(self):
        pass

    @abc.abstractmethod
    def stop_mining(self):
        pass

    @abc.abstractmethod
    def reboot(self, delay=0):
        pass

    @abc.abstractmethod
    def authenticate(self):
        pass

    @abc.abstractmethod
    def firmware_type(self) -> str:
        pass

    @abc.abstractmethod
    def set_power_limit(self, watts):
        """Set an external power cap in watts.

        EpicMinerAPI overrides this as a no-op (ePIC has no external power-limit
        knob).  BixbitMinerAPI maps it to the Bixbit set_user_power_limit cmd.
        """
        pass

    @abc.abstractmethod
    def supports_per_chip_tuning(self) -> bool:
        pass

    @abc.abstractmethod
    def has_external_power_limit(self) -> bool:
        pass

    @abc.abstractmethod
    def has_capabilities_endpoint(self) -> bool:
        pass

    @abc.abstractmethod
    def has_internal_perpetual_tune(self) -> bool:
        pass

    @abc.abstractmethod
    def hardware_topology(self) -> HardwareTopology:
        pass

    @abc.abstractmethod
    def tuning_strategy(self) -> str:
        """Return the tuning strategy this firmware uses.

        Currently ``'voltage_chip_tune'`` (sweep voltage, chip-tune at each
        step) or ``'wattage_search'`` (binary-search a wattage cap; firmware
        owns V/F internally). New strategies = new module under
        ``tuner_app/tuning_engine/`` and a new return value. Used by
        ``engine._run_inner`` for algorithm dispatch.
        """
        pass
