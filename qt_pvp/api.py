import os
import asyncio
import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, validator
from webdav3.client import Client
from webdav3.exceptions import RemoteResourceNotFound

from qt_pvp import functions as main_funcs
from qt_pvp.interest_merge_funcs import merge_overlapping_interests
from qt_pvp.cms_interface import functions as cms_funcs
from qt_pvp.data import settings
from qt_pvp import cms_gate_client
from main_operator import Main


API_KEY = os.environ.get("API_KEY")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

WEBDAV_OPTIONS = {
    "webdav_hostname": os.environ.get("webdav_hostname"),
    "webdav_login": os.environ.get("webdav_login"),
    "webdav_password": os.environ.get("webdav_password"),
}

TIME_FMT_FN = "%Y.%m.%d %H.%M.%S"   # для имён папок
TIME_FMT_DAY = "%Y.%m.%d"          # входной формат даты с точками
TIME_FMT = "%Y-%m-%d %H:%M:%S"     # для запросов CMS


async def verify_api_key(api_key: str = Security(api_key_header)):
    """Проверка API ключа из заголовка X-API-Key"""
    if not API_KEY:
        # Если API_KEY не установлен в .env - доступ открыт (dev mode)
        return True
    if api_key != API_KEY:
        raise HTTPException(
            status_code=403,
            detail="Invalid or missing API key"
        )
    return True


async def get_all_devices_from_cms(jsession: str) -> list[dict]:
    """Получает список всех устройств (онлайн и оффлайн) из CMS"""
    from qt_pvp.logger import logger

    try:
        devices = await cms_gate_client.list_devices(status="all")
        logger.info(f"[get_all_devices_from_cms] Got {len(devices)} devices from cms_gate")
        return devices
    except Exception as e:
        logger.error(f"[get_all_devices_from_cms] Error getting devices from cms_gate: {e}")
        return []


def get_reg_id_by_car_num_local(car_num: str) -> Optional[str]:
    """Поиск reg_id по госномеру машины в states.json"""
    try:
        from qt_pvp.data import settings
        import json
        
        with open(settings.states, 'r', encoding='utf-8') as f:
            states = json.load(f)
        
        regs = states.get('regs', {})
        for reg_id, reg_info in regs.items():
            plate = reg_info.get('plate', '')
            if not plate:
                continue
            plate = plate.upper().replace(' ', '')
            search_plate = car_num.upper().replace(' ', '')
            if plate == search_plate:
                return reg_id
        return None
    except Exception:
        return None


async def get_reg_id_by_car_num_cms(car_num: str, jsession: str) -> Optional[str]:
    """Поиск reg_id по госномеру через CMS API"""
    devices = await get_all_devices_from_cms(jsession)
    
    search_plate = car_num.upper().replace(' ', '').replace('-', '')
    
    # Список возможных префиксов
    prefixes = ['', 'ALG_', 'VOLVO_', 'KAMAZ_', 'MAN_', 'SCANIA_', 'MERCEDES_', 'OTK_', 'ZN2_', 'DES_']
    
    for device in devices:
        vid = device.get('vid', '').upper().replace(' ', '').replace('-', '')
        
        # Проверяем точное совпадение
        if vid == search_plate:
            return device.get('did')
        
        # Проверяем с удалением префиксов
        for prefix in prefixes:
            if vid.startswith(prefix):
                vid_without_prefix = vid[len(prefix):]
                if vid_without_prefix == search_plate:
                    return device.get('did')
            
            # Или наоборот - добавляем префикс к search_plate
            if vid == prefix + search_plate:
                return device.get('did')
    
    return None


async def resolve_reg_id(reg_id: Optional[str], car_num: Optional[str], jsession: Optional[str] = None) -> str:
    """Определяет reg_id из reg_id или car_num (сначала локально, затем через CMS)"""
    from qt_pvp.logger import logger
    
    if reg_id:
        return reg_id
    
    if car_num:
        # 1) Сначала ищем локально в states.json
        found_reg_id = get_reg_id_by_car_num_local(car_num)
        if found_reg_id:
            logger.info(f"[resolve_reg_id] Found in states.json: {car_num} -> {found_reg_id}")
            return found_reg_id

        # 2) Запрос в CMS (через cms_gate)
        found_reg_id = await get_reg_id_by_car_num_cms(car_num, jsession or "")
        if found_reg_id:
            logger.info(f"[resolve_reg_id] Found in CMS: {car_num} -> {found_reg_id}")
            return found_reg_id
        
        # 3) Last fallback: используем сам car_num как reg_id (может совпадать с DevIDNO)
        logger.info(f"[resolve_reg_id] Not found, using car_num as reg_id: {car_num}")
        return car_num
    
    raise HTTPException(
        status_code=422,
        detail="Either 'reg_id' or 'car_num' must be provided"
    )


def _validate_webdav_options():
    missing = [k for k, v in WEBDAV_OPTIONS.items() if not v]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing WebDAV env vars: {', '.join(missing)}")


def list_interest_folders(client: Client, base_path: str, plate: str, day_str: str) -> List[str]:
    day_path = f"{base_path}/{plate}/{day_str}"
    items = client.list(day_path)
    names = []
    for item in items:
        name = item.rstrip("/").split("/")[-1]
        if "_" in name and "-" in name and "." in name:
            names.append(name)
    return sorted(names)


def parse_folder_name(name: str):
    plate, rest = name.split("_", 1)
    left, right = rest.split("-")
    start_dt = datetime.datetime.strptime(left.strip(), TIME_FMT_FN)
    date_prefix = start_dt.strftime("%Y.%m.%d")
    end_dt = datetime.datetime.strptime(f"{date_prefix} {right.strip()}", TIME_FMT_FN)
    return plate, start_dt, end_dt


def fuzzy_equal(n1: str, n2: str, eps_sec: int = 10) -> bool:
    try:
        p1, s1, e1 = parse_folder_name(n1)
        p2, s2, e2 = parse_folder_name(n2)
    except Exception:
        return False
    return (
        p1 == p2 and
        abs((s1 - s2).total_seconds()) <= eps_sec and
        abs((e1 - e2).total_seconds()) <= eps_sec
    )


def diff_sets(expected, detected, eps_sec: int = 0):
    new = set(detected)
    missing = set(expected)
    if eps_sec <= 0:
        return new - expected, missing - detected

    matched_exp = set()
    matched_det = set()

    for e in expected:
        for d in detected:
            if d in matched_det:
                continue

            if e == d:
                matched_exp.add(e)
                matched_det.add(d)
                break

            if fuzzy_equal(e, d, eps_sec=eps_sec):
                matched_exp.add(e)
                matched_det.add(d)
                break

    new -= matched_det
    missing -= matched_exp
    return new, missing


class CompareRequest(BaseModel):
    reg_id: Optional[str] = Field(None, description="DevIDNO регистратора")
    car_num: Optional[str] = Field(None, description="Госномер автомобиля")
    day: str = Field(..., description="Дата в формате YYYY.MM.DD")
    base_path: str = Field("/Tracker/Видео выгрузок", description="Базовый путь на WebDAV")

    @validator("day")
    def _check_day(cls, v):
        try:
            datetime.datetime.strptime(v, TIME_FMT_DAY)
        except Exception as e:
            raise ValueError(f"day must be YYYY.MM.DD: {e}")
        return v
    
    @validator("car_num")
    def _check_reg_or_car(cls, v, values):
        if not values.get("reg_id") and not v:
            raise ValueError("Either reg_id or car_num must be provided")
        return v


class InterestRequest(BaseModel):
    reg_id: Optional[str] = Field(None, description="DevIDNO регистратора")
    car_num: Optional[str] = Field(None, description="Госномер автомобиля")
    start_time: str = Field(..., description="YYYY-MM-DD HH:MM:SS")
    end_time: str = Field(..., description="YYYY-MM-DD HH:MM:SS")
    merge_overlaps: bool = True

    @validator("start_time", "end_time")
    def _check_ts(cls, v):
        try:
            datetime.datetime.strptime(v, TIME_FMT)
        except Exception as e:
            raise ValueError(f"time must be {TIME_FMT}: {e}")
        return v
    
    @validator("car_num")
    def _check_reg_or_car(cls, v, values):
        if not values.get("reg_id") and not v:
            raise ValueError("Either reg_id or car_num must be provided")
        return v


class SiteItem(BaseModel):
    id: str
    lat: float
    lon: float


class StopsRequest(BaseModel):
    reg_id: Optional[str] = Field(None, description="DevIDNO регистратора")
    car_num: Optional[str] = Field(None, description="Госномер автомобиля")
    date: str = Field(..., description="Дата в формате YYYY-MM-DD")
    sites: List[SiteItem]
    radius_m: float = 120.0

    @validator("date")
    def _check_date(cls, v):
        try:
            datetime.datetime.strptime(v, "%Y-%m-%d")
        except Exception as e:
            raise ValueError(f"date must be YYYY-MM-DD: {e}")
        return v
    
    @validator("car_num")
    def _check_reg_or_car(cls, v, values):
        if not values.get("reg_id") and not v:
            raise ValueError("Either reg_id or car_num must be provided")
        return v


app = FastAPI(title="qt_pvp API")


async def _get_main_logged_in() -> Main:
    m = Main()
    await m.login()
    return m


@app.post("/compare-interests")
async def compare_interests(req: CompareRequest, authorized: bool = Depends(verify_api_key)):
    _validate_webdav_options()
    client = Client(WEBDAV_OPTIONS)

    # Логинимся в CMS для resolve и дальнейших запросов
    m = await _get_main_logged_in()
    reg_id = await resolve_reg_id(req.reg_id, req.car_num, m.jsession)
    reg_info = main_funcs.get_reg_info(reg_id) or {}
    plate = reg_info.get("plate") or reg_id

    try:
        folder_names = list_interest_folders(client, req.base_path, plate, req.day)
    except RemoteResourceNotFound:
        raise HTTPException(status_code=404, detail=f"Day {req.day} not found in cloud for plate {plate}")

    # Конвертация даты в формат CMS
    day_dt = datetime.datetime.strptime(req.day, TIME_FMT_DAY).date()
    start_time = f"{day_dt.strftime('%Y-%m-%d')} 00:00:00"
    stop_time = f"{day_dt.strftime('%Y-%m-%d')} 23:59:59"

    reg_info_full = main_funcs.get_reg_info(reg_id)
    interests = await m.get_interests_async(
        reg_id=reg_id,
        reg_info=reg_info_full,
        start_time=start_time,
        stop_time=stop_time,
    )
    interests = merge_overlapping_interests(interests)
    detected_names = set(i["name"] for i in interests)
    expected_names = set(folder_names)

    new_fuzzy, missing_fuzzy = diff_sets(expected_names, detected_names, eps_sec=30)

    return {
        "cloud_total": len(folder_names),
        "detected_total": len(interests),
        "new_not_in_cloud": sorted(new_fuzzy),
        "missing_in_detected": sorted(missing_fuzzy),
    }


@app.post("/get-interests")
async def get_interests_api(req: InterestRequest, authorized: bool = Depends(verify_api_key)):
    m = await _get_main_logged_in()
    reg_id = await resolve_reg_id(req.reg_id, req.car_num, m.jsession)
    reg_info_full = main_funcs.get_reg_info(reg_id)
    interests = await m.get_interests_async(
        reg_id=reg_id,
        reg_info=reg_info_full,
        start_time=req.start_time,
        stop_time=req.end_time,
    )
    if req.merge_overlaps:
        interests = merge_overlapping_interests(interests)
    return {"count": len(interests), "interests": interests}


@app.post("/find-stops")
async def find_stops_api(req: StopsRequest, authorized: bool = Depends(verify_api_key)):
    m = await _get_main_logged_in()
    reg_id = await resolve_reg_id(req.reg_id, req.car_num, m.jsession)
    res = await cms_funcs.find_stops_near_sites_by_date(
        reg_id=reg_id,
        sites=[s.dict() for s in req.sites],
        date=req.date,
        radius_m=req.radius_m,
        jsession=m.jsession,
    )
    return res


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("qt_pvp.api:app", host="0.0.0.0", port=8001, reload=False)

