"""Application settings. Secrets come only from the environment / .env."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    mode: Literal["mock", "testnet", "live"] = "mock"
    symbol: str = "BTCUSDT"
    host: str = "0.0.0.0"
    port: int = 8787
    app_token: Optional[str] = None
    mock_balance: float = 1000.0
    binance_api_key: Optional[str] = None
    binance_api_secret: Optional[str] = None
    allow_live: str = "no"
    ledger_path: Path = PROJECT_ROOT / "data" / "mock_ledger.json"
    watchlist_path: Path = PROJECT_ROOT / "data" / "watchlist.json"
    oplog_path: Path = PROJECT_ROOT / "data" / "ops.jsonl"
    # Per-browser state (watchlist, op log, mock ledger) lives under
    # clients_dir/<client_id>/. API keys are never stored on the server.
    clients_dir: Path = PROJECT_ROOT / "data" / "clients"
    max_clients: int = 2000
    rate_limit_per_minute: int = 240
    rate_limit_write_per_minute: int = 30
    ma_period: int = 20
    supertrend_atr_period: int = 10
    supertrend_multiplier: Decimal = Decimal("3")
    kline_cache_ttl: float = 15.0
    kline_limit: int = 200
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_divergence_lookback: int = 100
    # Cloud deploy knobs (see render.yaml). None of these touch risk rules.
    region_probe: str = "yes"  # startup check: can this egress IP reach fapi?
    ephemeral_storage: str = "no"  # yes = disk is wiped on restart/spin-down
    trust_proxy: str = "no"  # yes = behind a platform proxy; read client IP from headers

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = (value or "").strip().upper()
        if not symbol.isalnum():
            raise ValueError("SYMBOL 无效")
        return symbol

    @field_validator("ma_period", "supertrend_atr_period", "macd_fast", "macd_slow", "macd_signal", "macd_divergence_lookback")
    @classmethod
    def positive_period(cls, value: int) -> int:
        if int(value) <= 0:
            raise ValueError("指标周期必须为正整数")
        return int(value)

    @field_validator("supertrend_multiplier")
    @classmethod
    def positive_multiplier(cls, value: Decimal) -> Decimal:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError("Supertrend 乘数必须为正数")
        return number


    @model_validator(mode="after")
    def macd_fast_slower_than_slow(self) -> "Settings":
        if self.macd_fast >= self.macd_slow:
            raise ValueError("MACD_FAST 必须小于 MACD_SLOW")
        return self

    @property
    def auth_required(self) -> bool:
        return bool(self.app_token and self.app_token.strip())

    @property
    def region_probe_enabled(self) -> bool:
        return self.region_probe.strip().lower() == "yes"

    @property
    def is_ephemeral(self) -> bool:
        return self.ephemeral_storage.strip().lower() == "yes"

    @property
    def proxy_trusted(self) -> bool:
        return self.trust_proxy.strip().lower() == "yes"

    @property
    def live_allowed(self) -> bool:
        return self.allow_live.strip().lower() == "yes"

    def validate_startup(self) -> None:
        if self.mode == "live" and self.allow_live.strip().lower() != "yes":
            raise SystemExit(
                "拒绝启动：MODE=live 必须同时设置 ALLOW_LIVE=yes。"
                "这是额外的实盘保护，默认不会进入实盘。"
            )
        if self.mode in {"testnet", "live"}:
            if not self.binance_api_key or not self.binance_api_secret:
                raise SystemExit(
                    f"拒绝启动：MODE={self.mode} 需要在环境变量或 .env 中配置 "
                    "BINANCE_API_KEY 与 BINANCE_API_SECRET。"
                )


def load_settings() -> Settings:
    settings = Settings()
    settings.validate_startup()
    return settings
