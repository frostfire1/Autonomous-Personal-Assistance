from arq.connections import RedisSettings

from apa.core.settings import get_settings


def arq_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)
