import os
import time
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone
import requests
import math

# עקיפת חסימת הרשת של ווינדוס (לפיתוח מקומי)
if "SSLKEYLOGFILE" in os.environ:
    del os.environ["SSLKEYLOGFILE"]

app = FastAPI(title="DryRide API - Smart Routing & Weather (Secured)")

# 1. אבטחת CORS: רק האתר שלך מורשה לדבר עם השרת!
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://my-drop-123.netlify.app",
        "https://my-drop-123.netlify.app/"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- מודלים ---
class RouteRequest(BaseModel):
    origin: str
    destination: str
    departure_time: datetime

class WaypointWeather(BaseModel):
    location_name: str
    lat: float
    lon: float
    local_time: str
    driving_time_minutes: int
    weather_condition: str
    temperature: float
    precipitation_mm: float
    rain_chance_percent: int

class RouteSummary(BaseModel):
    total_distance_km: float
    total_duration_minutes: int
    origin_weather: WaypointWeather
    destination_weather: WaypointWeather
    max_rain_point: WaypointWeather | None = None
    weather_transitions: list[str] = []

class RouteResponse(BaseModel):
    summary: RouteSummary
    waypoints: list[WaypointWeather]

# --- מערכות אבטחה בזיכרון ---
GEO_CACHE = {}
IP_TRACKER = {}
MAX_REQUESTS_PER_MINUTE = 5

def check_rate_limit(request: Request):
    """מנגנון חסימת הצפות (Rate Limiter)"""
    # Render מעביר את ה-IP האמיתי דרך ההדר הזה
    forwarded = request.headers.get("X-Forwarded-For")
    client_ip = forwarded.split(",")[0] if forwarded else request.client.host

    now = time.time()
    
    # מנקה בקשות ישנות (מעל דקה) של אותו משתמש
    if client_ip in IP_TRACKER:
        IP_TRACKER[client_ip] = [t for t in IP_TRACKER[client_ip] if now - t < 60]
    else:
        IP_TRACKER[client_ip] = []

    # בודק אם עברנו את המכסה
    if len(IP_TRACKER[client_ip]) >= MAX_REQUESTS_PER_MINUTE:
        raise HTTPException(status_code=429, detail="ביצעת יותר מדי חיפושים. אנא המתן דקה ונסה שוב.")

    IP_TRACKER[client_ip].append(now)

    # הגנה על מנגנון ההגנה: אם הזיכרון מתמלא, מנקים אותו
    if len(IP_TRACKER) > 1000:
        IP_TRACKER.clear()

# --- מנגנון התעוררות ---
@app.get("/api/v1/ping")
def wake_up():
    return {"status": "awake"}

# --- פונקציות עזר מוגנות ---
def get_coordinates(city_name: str):
    if city_name in GEO_CACHE:
        return GEO_CACHE[city_name]
        
    # 2. קידוד בטוח של הכתובת (URL Encoding) למניעת הזרקות
    safe_city_name = quote(city_name)
    url = f"https://nominatim.openstreetmap.org/search?q={safe_city_name}&format=json&limit=1"
    headers = {"User-Agent": "DryRideApp/1.2"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if not data:
            raise ValueError(f"לא הצלחנו למצוא את המיקום: '{city_name}'. אנא בדוק את האיות.")
            
        lat, lon = float(data[0]['lat']), float(data[0]['lon'])
        
        # 3. מניעת פיצוץ זיכרון ב-Cache
        if len(GEO_CACHE) > 100:
            GEO_CACHE.clear()
            
        GEO_CACHE[city_name] = (lat, lon)
        return lat, lon
    except requests.exceptions.RequestException:
        raise ValueError("שירות חיפוש הערים אינו זמין כרגע. נסה שוב בעוד דקה.")

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0 
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c

def get_route_with_intervals(lat1: float, lon1: float, lat2: float, lon2: float):
    url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?geometries=geojson&overview=full"
    try:
        response = requests.get(url, timeout=15)
        data = response.json()
        
        if data.get("code") != "Ok":
            raise ValueError("לא הצלחנו לחשב מסלול נסיעה בין הנקודות.")
            
        route = data["routes"][0]
        total_distance_km = round(route["distance"] / 1000.0, 1)
        
        if total_distance_km > 600:
            raise ValueError(f"המסלול ארוך מדי ({total_distance_km} ק\"מ). המערכת תומכת במסלולים של עד 600 ק\"מ.")
            
        total_duration_sec = route["duration"]
        coordinates = route["geometry"]["coordinates"]
        
        cum_distances = [0.0]
        for i in range(1, len(coordinates)):
            pt1, pt2 = coordinates[i-1], coordinates[i]
            dist = haversine_distance(pt1[1], pt1[0], pt2[1], pt2[0])
            cum_distances.append(cum_distances[-1] + dist)
            
        actual_total_dist = cum_distances[-1] if cum_distances[-1] > 0 else 1
        waypoints = []
        interval_sec = 10 * 60 
        
        waypoints.append({"lat": coordinates[0][1], "lon": coordinates[0][0], "duration_from_start_sec": 0, "type": "התחלה"})
        
        current_target_sec = interval_sec
        for i, coord in enumerate(coordinates):
            fraction = cum_distances[i] / actual_total_dist
            time_at_coord = fraction * total_duration_sec
            
            if time_at_coord >= current_target_sec:
                waypoints.append({"lat": coord[1], "lon": coord[0], "duration_from_start_sec": current_target_sec, "type": f"אזור בדרך"})
                current_target_sec += interval_sec
                
        if waypoints[-1]["duration_from_start_sec"] < total_duration_sec:
            waypoints.append({"lat": coordinates[-1][1], "lon": coordinates[-1][0], "duration_from_start_sec": total_duration_sec, "type": "סיום"})
            
        return waypoints, total_distance_km, total_duration_sec
    except requests.exceptions.RequestException:
        raise ValueError("שירות הניווט אינו מגיב כרגע. נסה שוב מאוחר יותר.")

def get_real_weather(lat: float, lon: float, target_time_utc: datetime):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,precipitation_probability,weathercode",
        "timezone": "auto" 
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        
        offset_sec = data.get('utc_offset_seconds', 0)
        target_local_exact = target_time_utc + timedelta(seconds=offset_sec)
        exact_time_str = target_local_exact.strftime('%Y-%m-%dT%H:%M')
        target_hour_str = target_local_exact.strftime('%Y-%m-%dT%H:00')
        
        times_list = data.get('hourly', {}).get('time', [])
        time_index = times_list.index(target_hour_str)
        
        temp = data['hourly']['temperature_2m'][time_index]
        precip = data['hourly']['precipitation'][time_index]
        prob = data['hourly']['precipitation_probability'][time_index] 
        weather_code = data['hourly']['weathercode'][time_index]
        condition = "Rain" if weather_code >= 51 else "Clear/Cloudy"
        
        return {"condition": condition, "temp": temp, "precipitation": precip, "prob": prob, "local_time_str": exact_time_str}
    except (ValueError, KeyError, requests.exceptions.RequestException, IndexError):
        return {"condition": "Unknown", "temp": 0.0, "precipitation": 0.0, "prob": 0, "local_time_str": exact_time_str if 'exact_time_str' in locals() else "--:--"}

# הוספנו את ה-Request כפרמטר כדי לבדוק את ה-IP
@app.post("/api/v1/check-route", response_model=RouteResponse)
def check_route_weather(route_request: RouteRequest, req: Request):
    
    # 4. בודק אם המשתמש הציף את השרת לפני שהוא מעבד את הבקשה
    check_rate_limit(req)
    
    now_utc = datetime.now(timezone.utc)
    if route_request.departure_time < now_utc - timedelta(hours=1):
        raise HTTPException(status_code=400, detail="זמן היציאה לא יכול להיות בעבר.")
        
    try:
        lat1, lon1 = get_coordinates(route_request.origin)
        lat2, lon2 = get_coordinates(route_request.destination)
        
        raw_waypoints, total_distance, total_duration = get_route_with_intervals(lat1, lon1, lat2, lon2)
        duration_minutes = round(total_duration / 60)
        
        results = []
        for wp in raw_waypoints:
            driving_mins = int(wp["duration_from_start_sec"] / 60)
            eta_utc = route_request.departure_time + timedelta(seconds=wp["duration_from_start_sec"])
            weather_data = get_real_weather(wp["lat"], wp["lon"], eta_utc)
            
            loc_name = f"{wp['type']} (קואורדינטות {round(wp['lat'], 2)}, {round(wp['lon'], 2)})"
            if wp['type'] == "התחלה": loc_name = route_request.origin
            if wp['type'] == "סיום": loc_name = route_request.destination
            
            results.append(WaypointWeather(
                location_name=loc_name,
                lat=wp["lat"],
                lon=wp["lon"],
                local_time=weather_data["local_time_str"],
                driving_time_minutes=driving_mins,
                weather_condition=weather_data["condition"],
                temperature=weather_data["temp"],
                precipitation_mm=weather_data["precipitation"],
                rain_chance_percent=weather_data["prob"]
            ))
            
        origin_weather = results[0]
        destination_weather = results[-1]
        
        max_rain_point = None
        max_rain_chance = 0
        transitions = []
        is_currently_raining = origin_weather.rain_chance_percent > 0
        
        for wp in results:
            if wp.rain_chance_percent > max_rain_chance:
                max_rain_chance = wp.rain_chance_percent
                max_rain_point = wp
                
            is_raining_at_wp = wp.rain_chance_percent > 0
            if is_raining_at_wp and not is_currently_raining:
                transitions.append(f"כניסה לאזור עם סיכוי לגשם ({wp.rain_chance_percent}%) אחרי {wp.driving_time_minutes} דקות נסיעה.")
                is_currently_raining = True
            elif not is_raining_at_wp and is_currently_raining:
                transitions.append(f"יציאה מאזור הגשם אל אזור יבש אחרי {wp.driving_time_minutes} דקות נסיעה.")
                is_currently_raining = False
                
        summary = RouteSummary(
            total_distance_km=total_distance,
            total_duration_minutes=duration_minutes,
            origin_weather=origin_weather,
            destination_weather=destination_weather,
            max_rain_point=max_rain_point,
            weather_transitions=transitions
        )
        
        return RouteResponse(summary=summary, waypoints=results)
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
