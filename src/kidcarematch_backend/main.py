from fastapi import FastAPI

from kidcarematch_backend.api.v1.centres import router as centres_router
from kidcarematch_backend.api.v1.users import router as users_router

app = FastAPI()

app.include_router(users_router)
app.include_router(centres_router)


@app.get("/")
def read_root():
    return {"Hello": "World"}
