from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite:///:memory:"
    DB_ECHO: bool = False
    DATA_GOV_SG_API_URL: str = "https://data.gov.sg/api/action/datastore_search"
    DATA_GOV_SG_CENTRES_RESOURCE_ID: str = "d_696c994c50745b079b3684f0e90ffc53"
    DATA_GOV_SG_SERVICES_RESOURCE_ID: str = "d_44cfe12f2858ae503a093dfc075a28be"
    ONEMAP_API_URL: str = "https://www.onemap.gov.sg/api/common/elastic/search"
    FIREBASE_PROJECT_ID: str | None = None
    FIREBASE_CREDENTIALS_PATH: str | None = "./firebase_svc_acc.json"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
