from __future__ import annotations

from fastapi import APIRouter

from app.api import auth, files, packages, skills, system, taxonomy, tokens, users, versions
from app.services.mappers import API_PREFIX

router = APIRouter(prefix=API_PREFIX)

router.include_router(system.router)
router.include_router(auth.router)
router.include_router(skills.router)
router.include_router(versions.router)
router.include_router(files.router)
router.include_router(packages.router)
router.include_router(taxonomy.router)
router.include_router(tokens.router)
router.include_router(users.router)
