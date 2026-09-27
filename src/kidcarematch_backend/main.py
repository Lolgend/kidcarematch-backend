from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"Hello": "World"}

@app.get("/users/{user_id}")
def read_item(user_id: int, q: str | None = None):
    return {"user_id": user_id, "q": q}

@app.get("/reviews/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"user_id": item_id, "q": q}