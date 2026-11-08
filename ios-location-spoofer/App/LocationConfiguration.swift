//
//  LocationConfiguration.swift
//  location-spoofer
//
//  Shared configuration for location spoofing functionality
//

import Foundation
import os.log

struct Coordinates {
    let latitude: Double
    let longitude: Double
}

class LocationConfiguration: ObservableObject {
    static let shared = LocationConfiguration()
    
    private let userDefaults: UserDefaults
    private let suiteName = "group.com.waypoint.location-spoofer"
    
    private enum Keys {
        static let latitude = "spoofed_latitude"
        static let longitude = "spoofed_longitude"
        static let horizontalAccuracyMeters = "spoofed_horizontal_accuracy_meters"
    }
    
    private init() {
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            fatalError("Failed to create UserDefaults with suite name: \(suiteName)")
        }
        self.userDefaults = defaults
        userDefaults.synchronize()
    }
    
    var currentCoordinates: Coordinates? {
        let lat = userDefaults.double(forKey: Keys.latitude)
        let lon = userDefaults.double(forKey: Keys.longitude)
        
        guard userDefaults.object(forKey: Keys.latitude) != nil,
              userDefaults.object(forKey: Keys.longitude) != nil else {
            return nil
        }
        
        return Coordinates(latitude: lat, longitude: lon)
    }
    
    func setCoordinates(latitude: Double, longitude: Double) {
        userDefaults.set(latitude, forKey: Keys.latitude)
        userDefaults.set(longitude, forKey: Keys.longitude)
        userDefaults.synchronize()

        os_log("Coordinates updated: %.6f, %.6f", log: OSLog.default, type: .info, latitude, longitude)
    }

    func clearCoordinates() {
        userDefaults.removeObject(forKey: Keys.latitude)
        userDefaults.removeObject(forKey: Keys.longitude)
        userDefaults.synchronize()

        os_log("Coordinates cleared - transparent mode", log: OSLog.default, type: .info)
    }

    /// Radius in metres to advertise in the Wi-Fi location response; 0 preserves the source value.
    var horizontalAccuracyMeters: Int64 {
        get { Int64(userDefaults.integer(forKey: Keys.horizontalAccuracyMeters)) }
        set { userDefaults.set(max(0, newValue), forKey: Keys.horizontalAccuracyMeters); userDefaults.synchronize() }
    }
    
    func synchronize() {
        userDefaults.synchronize()
    }
}
