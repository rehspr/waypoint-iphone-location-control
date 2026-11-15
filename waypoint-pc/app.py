from __future__ import annotations

import json
import math
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "index.html"
HOST = "127.0.0.1"
PORT = 8765
UDID_RE = re.compile(r"^[A-Fa-f0-9-]{16,64}$")
_lock = threading.Lock()
_active_process: subprocess.Popen[str] | None = None
_active_file: Path | None = None
_active_state_file: Path | None = None
_active_udid: str | None = None
_active_session: dict[str, object] | None = None
_last_session: dict[str, object] | None = None
_last_worker_position: dict[str, object] | None = None
_last_worker_error = ""
_command_version = 0
_geocode_lock = threading.Lock()
_last_geocode = 0.0
_ip_location_cache: tuple[float, dict[str, object]] | None = None


def cli(*args: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pymobiledevice3", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def device_arg(udid: str) -> list[str]:
    if not UDID_RE.fullmatch(udid):
        raise ValueError("Choose a connected iPhone first.")
    return ["--udid", udid]


def read_devices() -> list[dict[str, str]]:
    # usbmux includes paired Wi-Fi devices when iOS Wi-Fi connections are on.
    # The same iPhone can appear once per transport, so merge by UDID.
    result = cli("usbmux", "list")
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Could not read USB devices.")
    try:
        # pymobiledevice3 emits Python-style ``\UXXXXXXXX`` escapes for some
        # device names on Windows; JSON only accepts lowercase ``\u`` escapes.
        stdout = re.sub(r"\\U([0-9a-fA-F]{8})", lambda match: chr(int(match.group(1), 16)), result.stdout)
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Unexpected device-list response from pymobiledevice3.") from exc
    devices: dict[str, dict[str, object]] = {}
    for item in raw:
        udid = str(item.get("UniqueDeviceID") or item.get("Identifier") or "")
        if not udid:
            continue
        device = devices.setdefault(udid, {
            "udid": udid,
            "name": str(item.get("DeviceName") or item.get("ProductType") or "iPhone"),
            "ios": str(item.get("ProductVersion") or ""),
            "connections": set(),
        })
        device["connections"].add(str(item.get("ConnectionType") or "USB"))
    result_devices: list[dict[str, str]] = []
    for device in devices.values():
        connections = device.pop("connections")
        order = [name for name in ("USB", "Network") if name in connections]
        label = " + ".join("Wi-Fi" if name == "Network" else name for name in order)
        result_devices.append({**device, "connection": label or "Unknown"})
    return result_devices


def mount_developer_image(udid: str) -> None:
    result = cli("mounter", "auto-mount", *device_arg(udid), timeout=300)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Could not prepare iOS developer services. Enable Developer Mode and retry.")


def walking_loop(lat: float, lon: float, radius_m: float, mode: str = "walking") -> list[list[float]]:
    """Ask OpenStreetMap routing for one closed mapped loop."""
    if radius_m < 60:
        raise ValueError("Mapped walking loops need a radius of at least 60 metres.")
    center_lat = math.radians(lat)
    phase = random.random() * math.tau
    coordinates = [(lon, lat)]
    # Keep stops within the selected radius; the mapped route follows sidewalks,
    # footpaths and streets between them. The router may need to detour locally.
    for i in range(5):
        angle = phase + math.tau * i / 5
        distance = radius_m * random.uniform(0.35, 0.55)
        dlat = distance * math.cos(angle) / 111_320
        dlon = distance * math.sin(angle) / (111_320 * max(0.01, math.cos(center_lat)))
        coordinates.append((lon + dlon, lat + dlat))
    coordinates.append((lon, lat))
    path = ";".join(f"{x:.7f},{y:.7f}" for x, y in coordinates)
    router = "https://routing.openstreetmap.de/routed-foot/route/v1/driving/" if mode == "walking" else "https://router.project-osrm.org/route/v1/driving/"
    url = router + quote(path, safe=",;") + "?overview=full&geometries=geojson&steps=false"
    request = Request(url, headers={"User-Agent": "WaypointPC/0.3 (local iPhone QA tool)", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Walking route lookup failed: {exc}. Turn off mapped walking routes to use offline wandering.") from exc
    routes = result.get("routes") or []
    if result.get("code") != "Ok" or not routes:
        raise RuntimeError("No mapped walking loop was found here. Try a larger radius or turn off mapped walking routes.")
    geometry = routes[0].get("geometry", {}).get("coordinates", [])
    if not 2 <= len(geometry) <= 20_000:
        raise RuntimeError("The walking service returned an unusable path. Try again or use offline wandering.")
    return [[float(p[0]), float(p[1])] for p in geometry]


def mapped_route(origin: tuple[float, float], destination: tuple[float, float], mode: str) -> list[list[float]]:
    """Return route geometry as [longitude, latitude] using the selected OSM router."""
    if mode not in {"walking", "driving"}:
        raise ValueError("Choose walking or driving directions.")
    router = "https://routing.openstreetmap.de/routed-foot/route/v1/driving/" if mode == "walking" else "https://router.project-osrm.org/route/v1/driving/"
    path = f"{origin[1]:.7f},{origin[0]:.7f};{destination[1]:.7f},{destination[0]:.7f}"
    request = Request(router + quote(path, safe=",;") + "?overview=full&geometries=geojson&steps=false", headers={"User-Agent": "WaypointPC/0.4 (local iPhone QA tool)", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{mode.title()} directions failed: {exc}") from exc
    routes = result.get("routes") or []
    geometry = routes[0].get("geometry", {}).get("coordinates", []) if routes else []
    if result.get("code") != "Ok" or not 2 <= len(geometry) <= 20_000:
        raise RuntimeError(f"No mapped {mode} route was found between those locations.")
    return [[float(p[0]), float(p[1])] for p in geometry]


def live_replace(udid: str, command: dict[str, object]) -> bool:
    """Update an existing worker through its control pipe without clearing the fix."""
    global _active_session
    with _lock:
        process = _active_process
        if not process or process.poll() is not None or _active_udid != udid or not process.stdin:
            return False
        try:
            process.stdin.write(json.dumps({"action": "replace", **command}) + "\n")
            process.stdin.flush()
        except (OSError, BrokenPipeError):
            return False
        _active_session = {**command.get("session", {}), "started_at": time.time()}
        return True


def live_settings(udid: str, settings: dict[str, object]) -> bool:
    """Apply settings to the active worker without resetting its route clock or fix."""
    global _active_session
    with _lock:
        process = _active_process
        if not process or process.poll() is not None or _active_udid != udid or not process.stdin:
            return False
        try:
            process.stdin.write(json.dumps({"action": "settings", **settings}) + "\n")
            process.stdin.flush()
        except (OSError, BrokenPipeError):
            return False
        if _active_session:
            _active_session = {**_active_session, **{key: value for key, value in settings.items() if key in {
                "radius_m", "travel_speed_mps", "roam_speed_mps", "break_every_s", "break_for_s", "arrival_delay_s", "roam_route", "mode"
            }}}
            _active_session["speed_mps"] = settings.get("roam_speed_mps", _active_session.get("speed_mps", 1.3))
        return True


def begin_override() -> int:
    global _command_version
    with _lock:
        _command_version += 1
        return _command_version


def override_is_current(version: int) -> bool:
    with _lock:
        return version == _command_version


def estimated_point(route: list[list[float]], distance: float) -> list[float]:
    if not route:
        raise ValueError("The active route has no coordinates.")
    if len(route) == 1:
        return [float(route[0][0]), float(route[0][1])]
    remaining = max(0.0, distance)
    for left, right in zip(route, route[1:]):
        segment = route_distance([left, right])
        if remaining <= segment:
            fraction = remaining / segment if segment else 0.0
            return [float(left[0]) + (float(right[0]) - float(left[0])) * fraction,
                    float(left[1]) + (float(right[1]) - float(left[1])) * fraction]
        remaining -= segment
    return [float(route[-1][0]), float(route[-1][1])]


def route_distance(points: list[list[float]]) -> float:
    total = 0.0
    for left, right in zip(points, points[1:]):
        lat1, lon1 = math.radians(float(left[1])), math.radians(float(left[0]))
        lat2, lon2 = math.radians(float(right[1])), math.radians(float(right[0]))
        dlat, dlon = lat2 - lat1, lon2 - lon1
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        total += 6_371_000 * 2 * math.asin(min(1, math.sqrt(haversine)))
    return total


def current_position(session: dict[str, object]) -> tuple[float, float] | None:
    """Estimate the active simulated point from its route and monotonic session clock."""
    live = read_worker_position()
    if live:
        return float(live["lat"]), float(live["lon"])
    mode = str(session.get("mode", ""))
    elapsed = max(0.0, time.time() - float(session.get("started_at", time.time())))
    trip = session.get("travel_route")
    if isinstance(trip, list) and len(trip) > 1:
        trip_length = route_distance(trip)
        trip_speed = float(session.get("travel_speed_mps", session.get("speed_mps", 1.3)))
        trip_seconds = trip_length / max(0.1, trip_speed)
        if elapsed < trip_seconds:
            return tuple(estimated_point(trip, elapsed * trip_speed)[::-1])
        elapsed -= trip_seconds
    route = session.get("roam_route") if mode == "roam" else session.get("travel_route")
    if not route:
        route = session.get("route")
    if not isinstance(route, list) or len(route) < 2:
        return None
    points = [(float(point[1]), float(point[0])) for point in route]
    lengths = [0.0]
    for a, b in zip(points, points[1:]):
        lengths.append(lengths[-1] + route_distance([[a[1], a[0]], [b[1], b[0]]]))
    speed = max(0.1, float(session.get("roam_speed_mps", session.get("speed_mps", 1.3))))
    if mode == "roam":
        every, rest = float(session.get("break_every_s", 0)), float(session.get("break_for_s", 0))
        if every > 0 and rest > 0:
            cycle = every + rest
            elapsed = math.floor(elapsed / cycle) * every + min(elapsed % cycle, every)
        distance = (elapsed * speed) % max(lengths[-1], 1)
    else:
        distance = min(elapsed * speed, lengths[-1])
    for i in range(1, len(points)):
        if lengths[i] >= distance:
            span = lengths[i] - lengths[i - 1]
            fraction = (distance - lengths[i - 1]) / span if span else 0
            return (points[i - 1][0] + (points[i][0] - points[i - 1][0]) * fraction,
                    points[i - 1][1] + (points[i][1] - points[i - 1][1]) * fraction)
    return points[-1]


def read_worker_position() -> dict[str, object] | None:
    global _last_worker_position
    with _lock:
        path = _active_state_file
        fallback = dict(_last_worker_position) if _last_worker_position else None
    if path:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(state.get("updated_at", 0)) < 8:
                _last_worker_position = state
                return state
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    return fallback


def local_roam_loop(lat: float, lon: float, radius_m: float) -> list[list[float]]:
    """Create and return the offline loop so the map previews the actual worker path."""
    points = [[lon, lat]]
    phase = random.random() * math.tau
    for i in range(8):
        distance = radius_m * random.uniform(0.25, 0.75)
        angle = phase + math.tau * i / 8 + random.uniform(-0.25, 0.25)
        point_lat = lat + distance * math.cos(angle) / 111_320
        point_lon = lon + distance * math.sin(angle) / (111_320 * max(0.01, math.cos(math.radians(lat))))
        points.append([point_lon, point_lat])
    points.append([lon, lat])
    return points


def set_location(
    udid: str, lat: float, lon: float, radius_m: float = 0,
    speed_mps: float = 1.3, break_every_s: float = 0, break_for_s: float = 0,
    use_walking_paths: bool = False, override_version: int | None = None,
) -> list[list[float]] | None:
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Latitude or longitude is outside its valid range.")
    devices = read_devices()
    if udid not in {d["udid"] for d in devices}:
        raise ValueError("That iPhone is no longer connected. Refresh the device list.")
    version = next(d["ios"] for d in devices if d["udid"] == udid)
    try:
        major = int(version.split(".", 1)[0])
    except ValueError:
        major = 17
    args = ["developer"]
    if major >= 17:
        mount_developer_image(udid)
        args += ["dvt"]
    args += ["simulate-location", "set", *device_arg(udid), "--", str(lat), str(lon)]
    if major < 17:
        result = cli(*args)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "The iPhone rejected the location request.")
        global _active_udid
        _active_udid = udid
        return None
    # Keep one DVT channel open and refresh the fix. iOS may drop a one-shot fix
    # after a short interval even while the CLI process remains alive.
    if not 0 <= radius_m <= 50_000:
        raise ValueError("Roaming radius must be between 0 and 50,000 metres.")
    if not 0.1 <= speed_mps <= 5.0:
        raise ValueError("Roaming speed must be between 0.1 and 5 metres per second.")
    if not 0 <= break_every_s <= 86_400 or not 0 <= break_for_s <= 86_400:
        raise ValueError("Break timers must be within one day.")
    route_path: list[list[float]] | None = None
    route_file: Path | None = None
    if radius_m > 0:
        route_path = walking_loop(lat, lon, radius_m) if use_walking_paths else local_roam_loop(lat, lon, radius_m)
        handle = tempfile.NamedTemporaryFile("w", suffix=".route.json", encoding="utf-8", delete=False)
        with handle:
            json.dump(route_path, handle)
        route_file = Path(handle.name)
    try:
        worker_command = {"target": [lon, lat], "travel_path": [[lon, lat]],
                          "roam_path": route_path or [], "teleport": True,
                          "mode": "roam" if radius_m > 0 else "fixed", "radius_m": radius_m, "speed_mps": speed_mps,
                          "travel_speed_mps": speed_mps, "roam_speed_mps": speed_mps,
                          "break_every_s": break_every_s, "break_for_s": break_for_s}
        session = {"mode": "roam" if radius_m > 0 else "fixed", "route": route_path or [], "lat": lat, "lon": lon, "radius_m": radius_m,
                   "speed_mps": speed_mps, "travel_speed_mps": speed_mps, "roam_speed_mps": speed_mps,
                   "break_every_s": break_every_s, "break_for_s": break_for_s}
        worker_command["session"] = session
        if override_version is not None and not override_is_current(override_version):
            raise RuntimeError("A newer movement command has taken control.")
        if live_replace(udid, worker_command):
            return route_path or None
        start_persistent_simulation(
            [sys.executable, str(ROOT / "location_worker.py"), udid, str(lat), str(lon), str(radius_m),
             str(speed_mps), str(break_every_s), str(break_for_s), str(route_file or "")],
            route_file, udid,
            session,
        )
        return route_path or None
    except Exception:
        if route_file:
            route_file.unlink(missing_ok=True)
        raise


def clear_location(udid: str) -> None:
    devices = read_devices()
    if udid not in {d["udid"] for d in devices}:
        raise ValueError("Reconnect the same iPhone before clearing its simulated location.")
    version = next(d["ios"] for d in devices if d["udid"] == udid)
    try:
        major = int(version.split(".", 1)[0])
    except ValueError:
        major = 17
    args = ["developer"] + (["dvt"] if major >= 17 else []) + ["simulate-location", "clear", *device_arg(udid)]
    result = cli(*args)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Could not clear simulated location.")


def stop_simulation(udid: str | None = None) -> None:
    global _active_process, _active_file, _active_state_file, _active_udid, _active_session, _last_worker_position
    with _lock:
        process, path, state_path = _active_process, _active_file, _active_state_file
        previous_udid = _active_udid
        _active_process = None
        _active_file = None
        _active_state_file = None
        _active_udid = None
        _active_session = None
        _last_worker_position = None
    if process and process.poll() is None:
        process.terminate()
        try:
            process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
    if path:
        path.unlink(missing_ok=True)
    if state_path:
        state_path.unlink(missing_ok=True)
    for target in {value for value in (previous_udid, udid) if value}:
        clear_location(target)


def start_persistent_simulation(
    command: list[str], route_file: Path | None = None, udid: str | None = None,
    session: dict[str, object] | None = None,
) -> None:
    global _active_process, _active_file, _active_state_file, _active_udid, _active_session, _last_worker_error, _last_session, _last_worker_position
    state_handle = tempfile.NamedTemporaryFile("w", suffix=".waypoint-state.json", delete=False, encoding="utf-8")
    state_file = Path(state_handle.name)
    state_handle.close()
    command = [*command, str(state_file)]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    time.sleep(0.8)
    if process.poll() is not None:
        _, error = process.communicate()
        if route_file:
            route_file.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)
        raise RuntimeError((error or "The iPhone location service exited before it could start.").strip())
    with _lock:
        _active_process, _active_file, _active_udid = process, route_file, udid
        _active_state_file = state_file
        _active_session = {**(session or {}), "started_at": time.time()}
        _last_session = None
        _last_worker_error = ""
        _last_worker_position = None


class Handler(BaseHTTPRequestHandler):
    server_version = "WaypointPC/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def respond(self, status: int, data: object) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/geocode":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0].strip()
            try:
                near_lat = float(params["near_lat"][0]) if "near_lat" in params else None
                near_lon = float(params["near_lon"][0]) if "near_lon" in params else None
                if near_lat is not None and (not -90 <= near_lat <= 90 or not -180 <= near_lon <= 180):
                    raise ValueError
            except (ValueError, IndexError):
                self.respond(400, {"error": "The nearby search center is invalid."})
                return
            if not query or len(query) > 160:
                self.respond(400, {"error": "Enter a place name to search."})
                return
            global _last_geocode
            try:
                with _geocode_lock:
                    delay = 1.0 - (time.monotonic() - _last_geocode)
                    if delay > 0:
                        time.sleep(delay)
                    search_url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=10&addressdetails=1&q=" + quote(query)
                    if near_lat is not None:
                        # Nominatim's viewbox biases results toward this area without excluding farther matches.
                        search_url += f"&viewbox={near_lon - 0.35:.5f},{near_lat + 0.25:.5f},{near_lon + 0.35:.5f},{near_lat - 0.25:.5f}"
                    request = Request(
                        search_url,
                        headers={"User-Agent": "WaypointPC/0.2 (local desktop location planner)"},
                    )
                    with urlopen(request, timeout=12) as response:
                        results = json.loads(response.read().decode("utf-8"))
                    _last_geocode = time.monotonic()
                normalized = []
                for item in results:
                    lat, lon = float(item["lat"]), float(item["lon"])
                    distance = None
                    if near_lat is not None:
                        dy = (lat - near_lat) * 111_320
                        dx = (lon - near_lon) * 111_320 * math.cos(math.radians(near_lat))
                        distance = round(math.hypot(dx, dy))
                    normalized.append({"name": item.get("name") or item.get("display_name", "Place"),
                                       "detail": item.get("display_name", "Place"), "lat": lat, "lon": lon,
                                       "distance_m": distance})
                if near_lat is not None:
                    normalized.sort(key=lambda item: item["distance_m"])
                self.respond(200, {"results": normalized})
            except Exception as exc:
                self.respond(502, {"error": f"Place search failed: {exc}"})
            return
        if route == "/api/ip-location":
            global _ip_location_cache
            cached = _ip_location_cache
            if cached and time.monotonic() - cached[0] < 1800:
                self.respond(200, cached[1])
                return
            try:
                request = Request("https://ipapi.co/json/", headers={"User-Agent": "WaypointPC/0.5 (local desktop map)", "Accept": "application/json"})
                with urlopen(request, timeout=8) as response:
                    item = json.loads(response.read().decode("utf-8"))
                lat, lon = float(item["latitude"]), float(item["longitude"])
                if not -90 <= lat <= 90 or not -180 <= lon <= 180 or item.get("error"):
                    raise ValueError("The IP location provider returned no usable location.")
                result = {"lat": lat, "lon": lon,
                          "label": ", ".join(part for part in (item.get("city"), item.get("region_code") or item.get("region"), item.get("country_code")) if part)}
                _ip_location_cache = (time.monotonic(), result)
                self.respond(200, result)
            except Exception as exc:
                self.respond(502, {"error": f"Could not estimate this network's location: {exc}"})
            return
        if route == "/api/devices":
            try:
                self.respond(200, {"devices": read_devices()})
            except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                self.respond(503, {"error": str(exc), "devices": []})
            return
        if route == "/api/state":
            global _last_session, _last_worker_error, _active_process, _active_file, _active_state_file, _active_udid, _active_session, _last_worker_position
            with _lock:
                active = bool(_active_process and _active_process.poll() is None)
                session = dict(_active_session) if active and _active_session else None
                process = _active_process
                previous_session = dict(_active_session) if _active_session else None
            if not active and process and process.poll() is not None:
                last_position = read_worker_position()
                try:
                    _, error = process.communicate(timeout=0)
                    _last_worker_error = (error or "").strip()[-3000:]
                except (subprocess.TimeoutExpired, OSError):
                    pass
                with _lock:
                    if _active_process is process:
                        _last_session = previous_session
                        _last_worker_position = last_position
                        _active_process = None
                        _active_file = None
                        _active_state_file = None
                        _active_udid = None
                        _active_session = None
            if session:
                live_position = read_worker_position()
                if live_position:
                    session["current_position"] = {"lat": live_position["lat"], "lon": live_position["lon"],
                                                   "phase": live_position.get("phase", live_position.get("mode", "fixed")),
                                                   "travelled_m": live_position.get("travelled_m", 0),
                                                   "roam_distance_m": live_position.get("roam_distance_m", 0),
                                                   "updated_at": live_position["updated_at"]}
            self.respond(200, {"routePlaying": active, "active": active, "session": session,
                               "lastSession": _last_session, "error": _last_worker_error})
            return
        if route in ("/", "/index.html"):
            try:
                content = WEB.read_bytes()
            except OSError:
                self.send_error(404, "Waypoint interface not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("Request is too large.")
            body = json.loads(self.rfile.read(length) or b"{}")
            route = urlparse(self.path).path
            udid = str(body.get("udid", ""))
            if route == "/api/location":
                override_version = begin_override()
                radius_m = float(body.get("radius_m", 0))
                speed_mps = float(body.get("speed_mps", 1.3))
                break_every_s = float(body.get("break_every_s", 0))
                break_for_s = float(body.get("break_for_s", 0))
                arrival_delay_s = float(body.get("arrival_delay_s", 0))
                use_walking_paths = bool(body.get("use_walking_paths", False))
                path_points = set_location(udid, float(body["lat"]), float(body["lon"]), radius_m, speed_mps, break_every_s, break_for_s, use_walking_paths, override_version)
                message = "Mapped walking roam started." if radius_m > 0 and use_walking_paths else "Radius roam started." if radius_m > 0 else "Location simulation started and will refresh while connected."
                self.respond(200, {"ok": True, "message": message, "route": path_points})
            elif route == "/api/go-to":
                override_version = begin_override()
                destination = (float(body["lat"]), float(body["lon"]))
                if not -90 <= destination[0] <= 90 or not -180 <= destination[1] <= 180:
                    raise ValueError("Latitude or longitude is outside its valid range.")
                mode = str(body.get("travel_mode", "walking"))
                teleport = bool(body.get("teleport", False))
                radius_m = float(body.get("radius_m", 0))
                travel_speed_mps = float(body.get("travel_speed_mps", body.get("speed_mps", 1.3)))
                roam_speed_mps = float(body.get("roam_speed_mps", body.get("speed_mps", 1.3)))
                break_every_s = float(body.get("break_every_s", 0))
                break_for_s = float(body.get("break_for_s", 0))
                arrival_delay_s = float(body.get("arrival_delay_s", 0))
                max_travel_speed = 5 if mode == "walking" else 45
                if not 0.1 <= travel_speed_mps <= max_travel_speed:
                    raise ValueError(f"{mode.title()} speed must be between 0.1 and {max_travel_speed} metres per second.")
                if not 0.1 <= roam_speed_mps <= 5 or not 0 <= radius_m <= 50_000:
                    raise ValueError("Check roaming speed and radius.")
                if not 0 <= break_every_s <= 86_400 or not 0 <= break_for_s <= 86_400:
                    raise ValueError("Break timers must be within one day.")
                if not 0 <= arrival_delay_s <= 3_600:
                    raise ValueError("Arrival pause must be between 0 and 3,600 seconds.")
                device = next((d for d in read_devices() if d["udid"] == udid), None)
                if not device:
                    raise ValueError("That iPhone is no longer connected. Refresh the device list.")
                if int((device["ios"] or "17").split(".", 1)[0]) < 17:
                    raise ValueError("Walking and driving simulation requires iOS 17 or newer.")
                mount_developer_image(udid)
                with _lock:
                    previous = dict(_active_session) if _active_session and _active_udid == udid else None
                if previous:
                    origin = current_position(previous) or (float(previous.get("lat", destination[0])), float(previous.get("lon", destination[1])))
                elif teleport:
                    origin = destination
                else:
                    if "origin_lat" not in body or "origin_lon" not in body:
                        raise ValueError("A first walking or driving trip needs a starting point. Allow PC location access in the browser or pick a start on the map.")
                    origin = (float(body["origin_lat"]), float(body["origin_lon"]))
                    if not -90 <= origin[0] <= 90 or not -180 <= origin[1] <= 180:
                        raise ValueError("Starting latitude or longitude is outside its valid range.")
                travel_path = [[destination[1], destination[0]]] if teleport else mapped_route(origin, destination, mode)
                use_paths = bool(body.get("use_paths", True))
                roam_path = (walking_loop(*destination, radius_m, mode) if use_paths else local_roam_loop(*destination, radius_m)) if radius_m > 0 else []
                if not override_is_current(override_version):
                    raise RuntimeError("A newer movement command took control while directions were loading. Select this destination again to retry.")
                session = {"mode": "roam" if radius_m > 0 else "travel", "route": roam_path or travel_path,
                           "roam_route": roam_path, "travel_route": travel_path, "transport": mode, "lat": destination[0], "lon": destination[1],
                           "radius_m": radius_m, "speed_mps": roam_speed_mps, "travel_speed_mps": travel_speed_mps,
                           "roam_speed_mps": roam_speed_mps, "break_every_s": break_every_s,
                           "break_for_s": break_for_s, "arrival_delay_s": arrival_delay_s}
                command = {"target": [destination[1], destination[0]], "travel_path": travel_path,
                           "roam_path": roam_path, "teleport": teleport, "radius_m": radius_m,
                           "mode": "roam" if radius_m > 0 else "fixed", "travel_speed_mps": travel_speed_mps,
                           "roam_speed_mps": roam_speed_mps, "speed_mps": roam_speed_mps,
                           "break_every_s": break_every_s, "break_for_s": break_for_s, "arrival_delay_s": arrival_delay_s,
                           "session": session}
                if not live_replace(udid, command):
                    # Starting without an existing session begins at the planned origin,
                    # then follows the same one-time route toward the destination.
                    initial = origin
                    if teleport:
                        initial = destination
                    start_persistent_simulation(
                        [sys.executable, str(ROOT / "location_worker.py"), udid, str(initial[0]), str(initial[1]), "0", str(roam_speed_mps), "0", "0", ""],
                        None, udid, session,
                    )
                    if not teleport:
                        live_replace(udid, command)
                self.respond(200, {"ok": True, "message": ("Teleported; roaming started." if teleport and radius_m else "Teleported to destination." if teleport else f"Following mapped {mode} directions; roaming starts on arrival." if radius_m else f"Following mapped {mode} directions to destination."), "route": travel_path, "roam_route": roam_path})
            elif route == "/api/settings":
                with _lock:
                    session = dict(_active_session) if _active_session and _active_udid == udid else None
                if not session:
                    raise ValueError("Start a simulated location before applying live movement settings.")
                radius_m = float(body.get("radius_m", session.get("radius_m", 0)))
                travel_speed_mps = float(body.get("travel_speed_mps", session.get("travel_speed_mps", 1.3)))
                roam_speed_mps = float(body.get("roam_speed_mps", session.get("roam_speed_mps", session.get("speed_mps", 1.3))))
                break_every_s = float(body.get("break_every_s", session.get("break_every_s", 0)))
                break_for_s = float(body.get("break_for_s", session.get("break_for_s", 0)))
                arrival_delay_s = float(body.get("arrival_delay_s", session.get("arrival_delay_s", 0)))
                use_paths = bool(body.get("use_paths", True))
                if not 0 <= radius_m <= 50_000 or not 0.1 <= travel_speed_mps <= 45 or not 0.1 <= roam_speed_mps <= 5:
                    raise ValueError("Check the radius, walking and driving speeds, and roaming speed ranges.")
                if not 0 <= break_every_s <= 86_400 or not 0 <= break_for_s <= 86_400 or not 0 <= arrival_delay_s <= 3_600:
                    raise ValueError("Break timers must be within one day.")
                roam_path: list[list[float]] = []
                if radius_m > 0:
                    center_lat, center_lon = float(session.get("lat", 0)), float(session.get("lon", 0))
                    if use_paths:
                        transport = str(session.get("transport", "walking"))
                        roam_path = walking_loop(center_lat, center_lon, radius_m, transport)
                    else:
                        roam_path = local_roam_loop(center_lat, center_lon, radius_m)
                mode = "roam" if radius_m > 0 else "travel" if session.get("travel_route") else "fixed"
                settings = {"mode": mode, "radius_m": radius_m, "travel_speed_mps": travel_speed_mps,
                            "roam_speed_mps": roam_speed_mps, "break_every_s": break_every_s,
                            "break_for_s": break_for_s, "arrival_delay_s": arrival_delay_s,
                            "roam_path": roam_path}
                if not live_settings(udid, settings):
                    raise RuntimeError("The active movement worker is no longer connected. The simulated phone fix was not cleared; start a new movement command to reconnect it.")
                self.respond(200, {"ok": True, "message": "Settings applied to the active simulation."})
            elif route == "/api/clear":
                stop_simulation(udid)
                self.respond(200, {"ok": True, "message": "Location simulation cleared."})
            elif route == "/api/route/start":
                override_version = begin_override()
                points = body.get("points")
                if not isinstance(points, list) or not 2 <= len(points) <= 500:
                    raise ValueError("Add at least two valid points to play a route.")
                checked = []
                for point in points:
                    lat, lon = float(point["lat"]), float(point["lon"])
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                        raise ValueError("A route point is outside its valid range.")
                    checked.append((lat, lon))
                devices = read_devices()
                device = next((d for d in devices if d["udid"] == udid), None)
                if not device:
                    raise ValueError("That iPhone is no longer connected. Refresh the device list.")
                if int((device["ios"] or "17").split(".", 1)[0]) < 17:
                    raise ValueError("Route playback requires iOS 17 or newer. A single location may work on earlier versions.")
                mount_developer_image(udid)
                with _lock:
                    previous = dict(_active_session) if _active_session and _active_udid == udid else None
                teleport = bool(body.get("teleport", False))
                if previous and not teleport:
                    origin = current_position(previous) or (checked[0][0], checked[0][1])
                    if math.hypot((origin[0] - checked[0][0]) * 111_000, (origin[1] - checked[0][1]) * 111_000) > 5:
                        checked.insert(0, origin)
                speed_mps = float(body.get("speed_mps", 4.0))
                route_points = [[lon, lat] for lat, lon in checked]
                if not override_is_current(override_version):
                    raise RuntimeError("A newer movement command took control. Select this route again to retry.")
                session = {"mode": "travel", "route": route_points, "travel_route": route_points,
                           "transport": "walking", "lat": checked[-1][0], "lon": checked[-1][1],
                           "speed_mps": speed_mps, "travel_speed_mps": speed_mps, "roam_speed_mps": speed_mps}
                command = {"target": route_points[-1], "travel_path": route_points, "roam_path": [],
                           "teleport": False, "mode": "fixed", "radius_m": 0,
                           "travel_speed_mps": speed_mps, "roam_speed_mps": speed_mps, "speed_mps": speed_mps,
                           "break_every_s": 0, "break_for_s": 0, "session": session}
                if not live_replace(udid, command):
                    start = checked[0]
                    start_persistent_simulation(
                        [sys.executable, str(ROOT / "location_worker.py"), udid, str(start[0]), str(start[1]), "0", str(speed_mps), "0", "0", ""],
                        None, udid, session,
                    )
                    live_replace(udid, command)
                self.respond(200, {"ok": True, "message": "Route playback started."})
            elif route == "/api/route/stop":
                begin_override()
                with _lock:
                    session = dict(_active_session) if _active_session and _active_udid == udid else None
                if not session:
                    raise ValueError("There is no active movement to pause.")
                current = current_position(session) or (float(session.get("lat", 0)), float(session.get("lon", 0)))
                point = [current[1], current[0]]
                held = {"mode": "fixed", "route": [], "travel_route": [], "roam_route": [],
                        "lat": current[0], "lon": current[1], "radius_m": 0, "speed_mps": 1.3,
                        "travel_speed_mps": session.get("travel_speed_mps", 1.3), "roam_speed_mps": 1.3}
                command = {"target": point, "travel_path": [point], "roam_path": [], "teleport": True,
                           "mode": "fixed", "radius_m": 0, "travel_speed_mps": 1.3,
                           "roam_speed_mps": 1.3, "break_every_s": 0, "break_for_s": 0, "session": held}
                if not live_replace(udid, command):
                    raise RuntimeError("The simulation worker exited. Its last error is available in Simulation status.")
                self.respond(200, {"ok": True, "message": "Movement paused. The current simulated location is held."})
            else:
                self.send_error(404)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            self.respond(400, {"error": str(exc)})


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    print(f"Waypoint PC is ready at {url}")
    print("Connect your iPhone by USB, unlock it, and tap Trust if prompted.")
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_simulation()


if __name__ == "__main__":
    main()
