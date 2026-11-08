"""Refresh simulated coordinates and optionally walk a mapped or local loop."""
from __future__ import annotations

import asyncio
import json
import math
import random
import sys
import time
import threading
from pathlib import Path

from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

EARTH_RADIUS_M = 6_371_000
REFRESH_SECONDS = 1.5


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1, math.sqrt(h)))


def random_point(lat: float, lon: float, radius_m: float) -> tuple[float, float]:
    distance = radius_m * math.sqrt(random.random())
    angle = random.random() * math.tau
    new_lat = lat + distance * math.cos(angle) / 111_320
    new_lon = lon + distance * math.sin(angle) / (111_320 * max(0.01, math.cos(math.radians(lat))))
    return max(-90, min(90, new_lat)), max(-180, min(180, new_lon))


def local_loop(lat: float, lon: float, radius_m: float, count: int = 8) -> list[tuple[float, float]]:
    """Create a gentle closed loop when the user opts out of network routing."""
    points = [(lat, lon)]
    phase = random.random() * math.tau
    for i in range(count):
        distance = radius_m * random.uniform(0.25, 0.75)
        angle = phase + math.tau * i / count + random.uniform(-0.25, 0.25)
        points.append(random_point(lat + distance * math.cos(angle) / 111_320,
                                   lon + distance * math.sin(angle) / (111_320 * max(0.01, math.cos(math.radians(lat)))),
                                   radius_m * 0.15))
    points.append((lat, lon))
    return points


def at_distance(points: list[tuple[float, float]], cumulative: list[float], travelled: float) -> tuple[float, float]:
    if not points or len(points) == 1 or cumulative[-1] <= 0:
        return points[0]
    travelled %= cumulative[-1]
    for i in range(1, len(cumulative)):
        if cumulative[i] >= travelled:
            segment = cumulative[i] - cumulative[i - 1]
            frac = (travelled - cumulative[i - 1]) / segment if segment else 0
            return (points[i - 1][0] + (points[i][0] - points[i - 1][0]) * frac,
                    points[i - 1][1] + (points[i][1] - points[i - 1][1]) * frac)
    return points[-1]


async def run(udid: str, lat: float, lon: float, radius_m: float, speed_mps: float,
              break_every_s: float, break_for_s: float, route_file: str, state_file: str = "") -> None:
    anchor = (lat, lon)
    route: list[tuple[float, float]] = []
    if radius_m > 0:
        if route_file:
            raw = json.loads(Path(route_file).read_text(encoding="utf-8"))
            route = [(float(p[1]), float(p[0])) for p in raw]
        else:
            route = local_loop(lat, lon, radius_m)
        if len(route) < 2:
            raise ValueError("The roaming path needs at least two points.")
    distances = [0.0]
    for i in range(1, len(route)):
        distances.append(distances[-1] + distance_m(route[i - 1], route[i]))

    async with UserspaceRsdTunnel(serial=udid) as rsd:
        async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as simulation:
            last_update = time.monotonic()
            paused_until = 0.0
            next_break = last_update + break_every_s if break_every_s > 0 and break_for_s > 0 else float("inf")
            fixed = anchor
            travelled = 0.0
            trip: list[tuple[float, float]] = []
            trip_distances: list[float] = []
            trip_length = 0.0
            trip_travelled = 0.0
            trip_target: tuple[float, float] | None = None
            loop_route = route
            loop_distances = distances
            trip_speed_mps = speed_mps
            roam_speed_mps = speed_mps
            moving_mode = "roam" if radius_m > 0 else "fixed"
            arrival_delay_s = 0.0
            arrival_wait_until = 0.0

            def receive_updates() -> None:
                for line in sys.stdin:
                    try:
                        command = json.loads(line)
                        if command.get("action") not in {"replace", "settings"}:
                            continue
                        updates.put(command)
                    except (json.JSONDecodeError, AttributeError):
                        continue

            import queue
            updates: queue.Queue[dict] = queue.Queue()
            threading.Thread(target=receive_updates, daemon=True).start()
            while True:
                now = time.monotonic()
                try:
                    while True:
                        command = updates.get_nowait()
                        if command.get("action") == "settings":
                            trip_speed_mps = float(command.get("travel_speed_mps", trip_speed_mps))
                            roam_speed_mps = float(command.get("roam_speed_mps", roam_speed_mps))
                            break_every_s = float(command.get("break_every_s", break_every_s))
                            break_for_s = float(command.get("break_for_s", break_for_s))
                            arrival_delay_s = float(command.get("arrival_delay_s", arrival_delay_s))
                            if arrival_wait_until:
                                arrival_wait_until = now + arrival_delay_s
                            radius_m = float(command.get("radius_m", radius_m))
                            if "roam_path" in command:
                                next_loop = [(float(p[1]), float(p[0])) for p in command.get("roam_path", [])]
                                if not trip and moving_mode == "roam" and len(next_loop) > 2:
                                    nearest = min(range(len(next_loop)), key=lambda i: distance_m(fixed, next_loop[i]))
                                    next_loop = [fixed, *next_loop[nearest:], *next_loop[1:nearest + 1], fixed]
                                loop_route = next_loop
                                loop_distances = [0.0]
                                for i in range(1, len(loop_route)):
                                    loop_distances.append(loop_distances[-1] + distance_m(loop_route[i - 1], loop_route[i]))
                                travelled = 0.0
                            if "mode" in command:
                                moving_mode = str(command["mode"])
                            next_break = now + break_every_s if break_every_s > 0 and break_for_s > 0 else float("inf")
                            continue
                        moving_mode = str(command.get("mode", "roam" if command.get("radius_m", 0) else "fixed"))
                        trip = [(float(p[1]), float(p[0])) for p in command.get("travel_path", [])]
                        trip_distances = [0.0]
                        for i in range(1, len(trip)):
                            trip_distances.append(trip_distances[-1] + distance_m(trip[i - 1], trip[i]))
                        trip_length = trip_distances[-1] if trip_distances else 0.0
                        trip_travelled = 0.0
                        trip_target = tuple(command["target"][::-1])
                        arrival_delay_s = float(command.get("arrival_delay_s", 0))
                        arrival_wait_until = 0.0
                        if command.get("teleport"):
                            fixed = trip_target
                            trip = []
                        loop_route = [(float(p[1]), float(p[0])) for p in command.get("roam_path", [])]
                        loop_distances = [0.0]
                        for i in range(1, len(loop_route)):
                            loop_distances.append(loop_distances[-1] + distance_m(loop_route[i - 1], loop_route[i]))
                        radius_m = float(command.get("radius_m", 0))
                        trip_speed_mps = float(command.get("travel_speed_mps", command.get("speed_mps", trip_speed_mps)))
                        roam_speed_mps = float(command.get("roam_speed_mps", command.get("speed_mps", roam_speed_mps)))
                        speed_mps = roam_speed_mps
                        break_every_s = float(command.get("break_every_s", 0))
                        break_for_s = float(command.get("break_for_s", 0))
                        travelled = 0.0
                        paused_until = 0.0
                        next_break = now + break_every_s if break_every_s > 0 and break_for_s > 0 else float("inf")
                except queue.Empty:
                    pass
                if not trip and moving_mode == "roam" and radius_m > 0 and now >= next_break:
                    paused_until = now + break_for_s
                    next_break = paused_until + break_every_s
                if not trip and moving_mode == "roam" and radius_m > 0 and now < paused_until:
                    current = fixed
                elif trip and trip_length > 0:
                    trip_travelled += max(0.0, now - last_update) * trip_speed_mps
                    current = at_distance(trip, trip_distances, min(trip_travelled, trip_length))
                    fixed = current
                    if trip_travelled >= trip_length:
                        fixed = trip_target or current
                        current = fixed
                        trip = []
                        if arrival_delay_s > 0:
                            arrival_wait_until = now + arrival_delay_s
                        if radius_m > 0 and len(loop_route) > 1:
                            moving_mode = "roam"
                elif moving_mode == "roam" and radius_m > 0 and len(loop_route) > 1:
                    if arrival_wait_until and now < arrival_wait_until:
                        current = fixed
                    else:
                        arrival_wait_until = 0.0
                    if arrival_wait_until:
                        pass
                    elif paused_until and now >= paused_until:
                        paused_until = 0.0
                    if not arrival_wait_until:
                        travelled += max(0.0, now - last_update) * roam_speed_mps
                        current = at_distance(loop_route, loop_distances, travelled)
                        fixed = current
                else:
                    current = fixed
                last_update = now
                await simulation.set(*current)
                if state_file:
                    state_path = Path(state_file)
                    temporary = state_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps({"lat": current[0], "lon": current[1], "mode": moving_mode,
                                                     "phase": "travel" if trip else "break" if now < paused_until else moving_mode,
                                                     "updated_at": time.time(), "travelled_m": trip_travelled,
                                                     "roam_distance_m": travelled}), encoding="utf-8")
                    temporary.replace(state_path)
                await asyncio.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) not in (9, 10):
        raise SystemExit("Usage: location_worker.py UDID LAT LON RADIUS_M SPEED_MPS BREAK_EVERY_S BREAK_FOR_S ROUTE_JSON [STATE_JSON]")
    asyncio.run(run(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]),
                    float(sys.argv[5]), float(sys.argv[6]), float(sys.argv[7]), sys.argv[8],
                    sys.argv[9] if len(sys.argv) == 10 else ""))
