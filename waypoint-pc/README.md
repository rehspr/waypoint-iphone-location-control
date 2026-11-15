# Waypoint PC

Waypoint PC is a local Windows controller for iPhone location simulation during app testing. It uses Apple's iOS developer location service through `pymobiledevice3`; it does not install a VPN, certificate, profile, or app on the phone.

## Run on Windows

1. Install the classic iTunes desktop installer (the Apple Mobile Device Support driver/service is needed for USB discovery).
2. Install Python 3.10 or newer.
3. Connect and unlock your iPhone once, then tap **Trust This Computer** if asked. For developer services, enable **Developer Mode** on the iPhone. Keep Wi-Fi connections enabled on the device (Waypoint can detect whether the network transport is available).
4. Double-click **Start Waypoint PC.bat**. It creates a local Python environment, installs the controller dependency, starts the app, and opens the page in your browser. Or run these commands from this folder:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\python -m pip install -r requirements.txt
   .\.venv\Scripts\python app.py
   ```

5. Open `http://127.0.0.1:8765` in a browser.

The host needs internet access the first time a Developer Disk Image must be downloaded. iOS 17.4 or newer is the supported Windows path; iOS 17.0–17.3.1 needs an additional elevated tunnel that this first version does not start. Earlier iOS releases have limited support. Device simulation is intended for development and QA; app behavior may vary, and it may not affect every location source or service.

### Wi-Fi connection

Pair and trust the iPhone over USB once, with **Wi-Fi connections** enabled. Then put the PC and iPhone on the same regular Wi-Fi network. Unplug the cable, press **Refresh**, and wait for the device to appear as **Wi-Fi** before starting a location or route session. Start the session after switching to Wi-Fi; unplugging a session that was opened over USB can interrupt it. Guest Wi-Fi or router client isolation can block discovery. On this Windows setup, `pymobiledevice3` currently sees the paired iPhone on both USB and Wi-Fi.

## Current controls

- Detect trusted iPhones over USB or paired Wi-Fi, merging both transports into one device entry.
- Navigate a map centered workspace with separate controls for navigation, teleporting, roaming, movement settings, and the live simulation.
- Choose a destination on the map, by place search, or with coordinates. Walk or drive along a mapped one-way route, then hold at arrival or begin a separate repeating roam loop.
- Search place names with nearby results ranked first. Search uses the active simulated position, chosen start point, or—if available—the browser's current location; results show distance from that center.
- Switch between OpenStreetMap street tiles and satellite imagery from Esri/its imagery providers.
- Satellite imagery includes a road, boundary, and place-label overlay. Search is biased to the approximate public IP location. A separate toggle shows result distance from that estimate and a user-adjustable visual radius.
- When there is no active simulation, a first Walk or Drive uses this PC's browser-provided current location for the starting point. Allow location access in the browser or pick the start on the map. When a simulation is active, the trip starts from the live simulated fix instead.
- Teleport directly to the selected point or turn on teleport for future destinations. New movement commands replace the active task while the iOS developer-service connection and current simulated fix stay alive.
- Set walking, driving, and roaming speeds independently. Adjust roaming radius, add scheduled roam breaks, and optionally pause at the destination before roaming starts.
- Choose mapped walking/road loops or offline wandering. Offline wandering moves in straight segments and can cross buildings.
- Play a user-defined series of stops, export the planned route as GPX, pause movement while holding the current fix, or explicitly clear the simulated location.
- Save movement preferences in this PC's browser.

The controller binds only to `127.0.0.1`. Street tiles and place search use OpenStreetMap services. Satellite imagery and its road/place overlays use Esri and its imagery/data providers. The IP estimate lookup sends the host's public IP to ipapi.co; place searches send the search text and estimated nearby center to Nominatim. The displayed radius is a user-selected visual range and is not an accuracy radius supplied by ipapi.co. Device identifiers are not sent to these services. Apple MapKit JS requires an Apple Maps token, so the app uses a labeled satellite layer without an embedded Apple token. The simulated location and roaming continue while Waypoint and the iOS developer connection remain active. A Wi-Fi developer connection removes the cable requirement, but the PC and phone must remain on the same reachable network and Waypoint must keep running; this is not the on-device VPN method.
