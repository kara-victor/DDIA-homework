from threading import Lock

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from metrics_collection_lib import MetricsMiddleware

from fastapi.responses import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

app = FastAPI()
app.add_middleware(MetricsMiddleware)

app.state.characters = {}
app.state.next_id = 1
app.state.lock = Lock()

classes = {"priest", "rogue", "wizard", "warrior"}
types = {"human", "elf", "dwarf", "orc"}
sexes = {"male", "female"}


class Character(BaseModel):
    name: str
    level: int = 1
    character_class: str
    character_type: str
    description: str | None = None
    sex: str


# Validation functions
def validate_character_class(character_class: str):
    return character_class in classes


def validate_character_type(character_type: str):
    return character_type in types


def validate_character_level(character_level: int):
    return 1 <= character_level <= 20


def validate_character_sex(character_sex: str):
    return character_sex in sexes


def validate_character(character: Character):
    if not validate_character_class(character.character_class):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid character class. Choose: {', '.join(sorted(classes))}"
        )

    if not validate_character_type(character.character_type):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid character type. Choose: {', '.join(sorted(types))}"
        )

    if not validate_character_level(character.level):
        raise HTTPException(
            status_code=400,
            detail="Invalid character level. Choose a value from 1 to 20"
        )

    if not validate_character_sex(character.sex):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid character sex. Choose: {', '.join(sorted(sexes))}"
        )


# Health check
@app.get("/health")
def health():
    return {"status": "ok"}


# Create character
@app.post("/characters", status_code=201)
def create_character(character: Character):
    validate_character(character)

    with app.state.lock:
        character_id = app.state.next_id
        app.state.characters[character_id] = character.model_dump()
        app.state.next_id += 1

    return {"id": character_id}


# Get character
@app.get("/characters/{character_id}")
def get_character(character_id: int):
    with app.state.lock:
        if character_id not in app.state.characters:
            raise HTTPException(
                status_code=404,
                detail="Character not found"
            )

        return app.state.characters[character_id].copy()


# Update character
@app.put("/characters/{character_id}")
def update_character(character_id: int, character: Character):
    validate_character(character)

    with app.state.lock:
        if character_id not in app.state.characters:
            raise HTTPException(
                status_code=404,
                detail="Character not found"
            )

        app.state.characters[character_id] = character.model_dump()

    return {"id": character_id}

@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    registry = request.state.metrics_registry

    return Response(
        content=generate_latest(registry),
        media_type=CONTENT_TYPE_LATEST
    )