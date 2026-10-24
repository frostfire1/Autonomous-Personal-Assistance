from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    timezone: str = "Asia/Jakarta"

    database_url: str = "postgresql+asyncpg://apa_app:change-me-app@localhost:5432/apa"
    redis_url: str = "redis://localhost:6379/0"

    litellm_base_url: str = "http://localhost:4000/v1"
    litellm_master_key: str = ""

    telegram_bot_token: str = ""
    telegram_owner_id: int = 0

    ingest_api_key: str = ""
    ingest_hmac_secret: str = ""  # optional: when set, every ingest call must carry a fresh HMAC signature
    ingest_rate_per_min: int = 120  # calls per minute across the API key; 0 turns the cap off

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket_evidence: str = "evidence"
    minio_region: str = ""  # Garage: "garage" (its default s3_region)
    minio_secure: bool = False  # TLS to MinIO; it sits on the private network behind the VPN

    # Receipt/evidence images. 'local' (a directory) for dev and tests, 's3' in production (Garage).
    evidence_backend: str = "local"
    evidence_dir: str = "data/evidence"
    max_image_bytes: int = 10 * 1024 * 1024  # Telegram photos are at most ~10 MB
    # A receipt album: a receipt + its payment slip, or the pages of a long receipt.
    max_album_images: int = 4
    # Album photos arrive as separate, racing messages; read only once they have stopped arriving.
    album_settle_seconds: int = 3

    sheets_credentials_file: str = ""
    sheets_spreadsheet_id: str = ""  # or paste the whole sheet link into SHEETS_URL
    sheets_url: str = ""

    # Fernet key that encrypts stored OAuth tokens (blueprint v2 4.5). Without it nothing secret is stored.
    secrets_key: str = ""
    nvd_api_key: str = ""  # optional: lifts NVD's rate limit (free key from nvd.nist.gov)
    price_feed_url: str = ""  # optional JSON feed of staple prices: [{"code","region","date","price"}]
    huawei_client_id: str = ""
    huawei_client_secret: str = ""
    # YouTube Data API (liked videos, follows): a Google Cloud OAuth client for TVs and Limited Input devices
    youtube_client_id: str = ""
    youtube_client_secret: str = ""

    # The briefing (voice clips + Telegram text) is prepared this many minutes before the wake-up time.
    briefing_wake_time: str = "06:30"
    briefing_lead_minutes: int = 25

    weather_city: str = "Jakarta"  # the weather is for this city until you choose another in the app or web
    weather_enabled: bool = True  # off: the briefing simply has no weather line
    firecrawl_api_key: str = ""  # optional: web search for the /private companion (firecrawl.dev)
    firecrawl_url: str = "https://api.firecrawl.dev"  # or the address of your own Firecrawl
    brave_api_key: str = ""  # optional: web search for the /private companion (brave.com/search/api)

    # Unrecognised bank notifications that may be sent to the model per hour (recognised ones never are).
    notification_llm_per_hour: int = 30
    # Telegram says so when the phone app has not checked in for this many hours
    phone_silent_hours: int = 2
    # Caring check-ins in Telegram (sleep, meals, late nights, caffeine, stress): at most 2 a day
    care_checkins: bool = True
    # The assistant starts a chat now and then (morning, midday, evening) and stops if not answered
    companion_chats: bool = True
    # GIF reactions in the chat (one of these keys switches it on): developers.giphy.com or Tenor
    giphy_api_key: str = ""
    tenor_api_key: str = ""
    # Web research by GPT Researcher (RESEARCH_MODE=bundled starts it): address, seconds to wait, a day
    research_url: str = ""
    research_timeout_s: int = 900
    research_per_day: int = 5
    companion_max_per_day: int = 3
    # Raw chat text older than this many days is erased once the diary has captured it (0 = keep for ever)
    chat_keep_days: int = 7

    # Safety defaults; the live values come from policy_rules (Web UI > Settings)
    undo_window_hours: int = 24

    # Osiris rejects parallel bursts (RateLimitError on 2 of 5 calls) long before its stated
    # 30 requests/minute, so cap in-flight LLM calls per process.
    llm_max_concurrency: int = 3
    # Briefing voice (text leaves the server, see tts/voice.py): Gemini if a key is set, else Microsoft.
    tts_enabled: bool = True
    tts_voice: str = "id-ID-GadisNeural"  # id-ID-ArdiNeural is the male voice
    tts_engine: str = "auto"  # auto = Gemini when GEMINI_API_KEY is set, else Microsoft; or "edge" / "gemini"
    gemini_api_key: str = ""  # free key from aistudio.google.com/apikey
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"
    gemini_tts_voice: str = "Zephyr"  # bright; also Kore (firm), Puck (upbeat), Leda (youthful)
    gemini_tts_style: str = (
        "Bacakan dalam bahasa Indonesia dengan nada ceria, hangat, dan bersemangat seperti pembawa acara "
        "pagi, dengan intonasi yang hidup dan tempo yang natural:"
    )
    tts_rate: str = "+8%"  # a little brisk reads as energetic
    tts_pitch: str = "+2Hz"
    # Providers rate-limit PER KEY: kunciai (primary) refused calls above ~60/min, Osiris (fallback): 30.
    # One debate is 5 requests, so a burst of transactions hits it, the proxy cools the rate-limited models
    # down for 60 s, and every fallback on the same key is refused too. Queue client-side instead of
    # colliding, with headroom under kunciai's ceiling for fallback hops this client cannot see.
    # 0 disables the limiter. Lower it (about 24) if Osiris becomes the primary.
    llm_max_rpm: int = 48
    # Blueprint v2 4.2 (confidence-gated debate): a merchant whose category an earlier debate settled,
    # verified within the TTL and now charging a normal amount is categorised from the merchant cache with
    # no model call. Breakers always run first; anything unusual (new merchant, odd amount, receipt, split,
    # over budget) still gets the full debate.
    fast_path_enabled: bool = True
    fast_path_min_visits: int = 3
    fast_path_max_sigma: float = 2.0
    fast_path_ttl_days: int = 30
    # Votes (of the 5 debaters) needed before a transaction may be committed.
    debate_quorum: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
