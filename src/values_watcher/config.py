"""Carga de configuración: config.yaml + variables de entorno (.env)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class OrderbookConfig(BaseModel):
    depth_levels: int = 20
    wall_multiplier: float = 5.0
    imbalance_threshold: float = 0.6


class AlertsConfig(BaseModel):
    enabled: bool = True       # master switch: False pausa todo envío de alertas
    dedup_minutes: int = 30
    enabled_events: list[str] = ["fvg_new", "fvg_mitigated", "wall", "imbalance"]


class KiyotakaConfig(BaseModel):
    enabled: bool = True                 # False → solo consultas bajo demanda (comandos del bot)
    poll_seconds: int = 60
    interval: str = "MINUTE"
    period: int = 1140
    max_depth: int = 1000
    block_size_target: int | None = None   # p.ej. 25 → bloques de ~25 USD
    large_order_thresholds: dict[str, float] = {}  # volumen mínimo por símbolo (p.ej. BTCUSDT: 300)
    critical_multiplier: float = 3.0       # volumen ≥ umbral × N → severidad critical
    order_blocks_enabled: bool = True      # evento resumen de bloques grandes
    order_blocks_interval_minutes: int = 15
    order_blocks_range_usd: float = 2000   # rango alrededor del precio actual
    order_blocks_min_volume: float = 300   # volumen mínimo por bloque (BTC)


class StorageConfig(BaseModel):
    db_path: str = "data/values_watcher.db"


class LiquidationsConfig(BaseModel):
    bucket_usd: float = 50           # ancho del bucket de precio para clusters
    min_alert_usd: float = 50_000    # alerta por liquidación individual ≥ este USD
    critical_multiplier: float = 5.0 # ≥ 5× umbral → critical
    window_hours: int = 24           # ventana del cluster en /api/liquidations


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class WatchRule(BaseModel):
    price_targets: list[float] = []   # niveles de precio a vigilar (cruces)
    stop_volume: float | None = None  # volumen absoluto mínimo de una pared en el libro vivo
    price_ladders: list[dict] = []    # escaleras: [{"level": 60000, "step_pct": 1.0, "direction": "below"}]


class AppConfig(BaseModel):
    symbols: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    timeframes: list[str] = ["5m", "15m"]
    orderbook: OrderbookConfig = OrderbookConfig()
    alerts: AlertsConfig = AlertsConfig()
    kiyotaka: KiyotakaConfig = KiyotakaConfig()
    storage: StorageConfig = StorageConfig()
    liquidations: LiquidationsConfig = LiquidationsConfig()
    api: ApiConfig = ApiConfig()
    watch: dict[str, WatchRule] = {}  # reglas por símbolo


class Settings(BaseSettings):
    """Secretos y endpoints, desde .env o entorno."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8")

    kiyotaka_api_key: str = ""
    notify_api_url: str = ""
    notify_api_key: str = ""
    notify_auth_header: str = ""  # default: "Authorization: Bearer {key}"
    telegram_bot_token: str = ""  # bot de comandos (BotFather)
    telegram_chat_id: str = ""    # chat autorizado para comandos


def load_config(path: Path | None = None) -> AppConfig:
    path = path or ROOT / "config.yaml"
    if path.exists():
        return AppConfig.model_validate(yaml.safe_load(path.read_text()) or {})
    return AppConfig()


def load_settings() -> Settings:
    return Settings()
