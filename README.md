# Waypoint

Waypoint is a Windows desktop controller for iPhone location simulation during app development and QA. It combines a map-based browser interface with Apple's iOS developer location service. It does not install an app, VPN, certificate, or profile on the iPhone.

## Components

- `waypoint-pc/` — Windows controller, setup script, and usage notes.
- `index.html` — map-centered control interface served locally by Waypoint PC.
- `ios-location-spoofer/` — upstream iOS app source included as a separate project; see its README and license.

## Start Waypoint PC

See [waypoint-pc/README.md](waypoint-pc/README.md) for requirements and setup. The controller supports paired USB or Wi-Fi devices, one-way walking/driving trips, teleporting, optional roaming, and route playback. The iOS developer service must stay connected while the simulated location is active.

For a first walking or driving trip, the browser needs access to this PC's current location to establish the trip origin. You can also pick the origin on the map. Active trips use the current simulated position as their origin.

## Privacy

The local controller binds to `127.0.0.1`. Map tiles, place search, and mapped routes use OpenStreetMap services and send the searched place or selected route coordinates to those services. Review each component's README for further details.

## License

See the license file in each component. The projects may have different licensing terms.
