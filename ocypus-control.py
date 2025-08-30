#!/usr/bin/env python3
"""
ocypus-control.py
---------------------------------
Ocypus Iota A40 LCD driver (Linux / Proxmox)

An improved, object-oriented version for better structure and robustness.

FEATURES
  • Auto-detects the working HID interface.
  • Supports temperature display in Celsius (°C) and Fahrenheit (°F).
  • Works with any psutil sensor.
  • Keeps the panel alive with periodic updates.
  • Includes a command to generate and install a systemd service.
"""

import argparse
import hid
import os
import signal
import sys
import textwrap
import time
from types import FrameType
from typing import List, Dict, Any, Optional, Tuple

import psutil

# --- Constants ---
VID, PID = 0x1a2c, 0x434d
REPORT_ID = 0x07
REPORT_LENGTH = 64
DEFAULT_SENSOR_SUBSTR = "coretemp"
DEFAULT_REFRESH_RATE = 1.0
KEEPALIVE_INTERVAL = 2.0


class OcypusController:
    """Manages the Ocypus Iota A40 LCD device."""

    def __init__(self):
        self.device: Optional[hid.device] = None
        self.interface_number: Optional[int] = None

    def __enter__(self):
        """Context manager entry: opens the device."""
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: closes the device."""
        self.close()

    def open(self) -> bool:
        """Opens the first working Ocypus device interface."""
        devices = hid.enumerate(VID, PID)
        if not devices:
            print("No Ocypus cooler found.")
            return False

        for device_info in devices:
            interface_number = device_info.get('interface_number')
            if interface_number is None:
                continue

            try:
                device = hid.device()
                device.open_path(device_info['path'])
                
                self.device = device
                self.interface_number = interface_number
                print(f"Connected to Ocypus cooler on interface {interface_number}")
                return True
            except Exception as e:
                print(f"Failed to open interface {interface_number}: {e}")
                try:
                    device.close()
                except:
                    pass
                continue

        print("Error: No working Ocypus interface found.")
        return False

    def close(self):
        """Closes the device connection."""
        if self.device:
            try:
                self.device.close()
            except Exception as e:
                print(f"Error closing device: {e}")
            finally:
                self.device = None
                self.interface_number = None

    def send_temperature(self, temp_celsius: float, unit: str = 'c') -> bool:
        """Sends temperature data to the LCD display."""
        if not self.device:
            print("Device not connected.")
            return False

        # Convert temperature based on unit
        if unit.lower() == 'f':
            display_temp = temp_celsius * 9/5 + 32
        else:
            display_temp = temp_celsius

        display_temp = max(0, min(212, int(round(display_temp))))
        hundreds = display_temp // 100
        tens = (display_temp % 100) // 10
        ones = display_temp % 10

        try:
            report = [REPORT_ID] + [0] * (REPORT_LENGTH - 1)
            report[1] = 0xff
            report[2] = 0xff
            report[3] = hundreds
            report[4] = tens
            report[5] = ones
            
            self.device.write(bytes(report))
            return True
        except Exception as e:
            print(f"Error sending temperature: {e}")
            return False

    def blank_display(self) -> bool:
        """Blanks the LCD display."""
        if not self.device:
            print("Device not connected.")
            return False

        try:
            report = [REPORT_ID] + [0] * (REPORT_LENGTH - 1)
            self.device.send_feature_report(report)
            return True
        except Exception as e:
            print(f"Error blanking display: {e}")
            return False

    def list_devices(self) -> List[Dict[str, Any]]:
        """Lists all Ocypus devices found."""
        devices = hid.enumerate(VID, PID)
        return devices


def get_temperature_sensors() -> Dict[str, List[Tuple[str, float]]]:
    """Gets all available temperature sensors."""
    try:
        return psutil.sensors_temperatures()
    except Exception as e:
        print(f"Error reading temperature sensors: {e}")
        return {}


def find_sensor_by_substring(sensors: Dict[str, List[Tuple[str, float]]], 
                           substring: str) -> Optional[Tuple[str, float]]:
    """Finds the first sensor containing the given substring."""
    for sensor_name, sensor_list in sensors.items():
        if substring.lower() in sensor_name.lower() and sensor_list:
            return sensor_name, sensor_list[0].current
    return None


def build_temperature_report(sensor_substring: str = DEFAULT_SENSOR_SUBSTR) -> str:
    """Builds a temperature report for debugging."""
    sensors = get_temperature_sensors()
    if not sensors:
        return "No temperature sensors found."
    
    report_lines = ["Available temperature sensors:"]
    for sensor_name, sensor_list in sensors.items():
        for sensor in sensor_list:
            temp_str = f"{sensor.current:.1f}°C"
            highlight = " ← SELECTED" if sensor_substring.lower() in sensor_name.lower() else ""
            report_lines.append(f"  {sensor_name}: {temp_str}{highlight}")
    
    return "\n".join(report_lines)


def run_display_loop(controller: OcypusController, 
                    sensor_substring: str, 
                    unit: str, 
                    refresh_rate: float):
    """Runs the main temperature display loop."""
    print(f"Starting temperature display (unit: {unit.upper()}, refresh: {refresh_rate}s)")
    print("Press Ctrl+C to stop.")
    
    last_keepalive = time.time()
    
    while True:
        try:
            # Get temperature
            sensors = get_temperature_sensors()
            sensor_data = find_sensor_by_substring(sensors, sensor_substring)
            
            if sensor_data:
                sensor_name, temp_celsius = sensor_data
                success = controller.send_temperature(temp_celsius, unit)
                if success:
                    display_temp = temp_celsius if unit.lower() == 'c' else temp_celsius * 9/5 + 32
                    unit_symbol = '°C' if unit.lower() == 'c' else '°F'
                    print(f"\rSensor: {sensor_name} | Temp: {display_temp:.1f}{unit_symbol}", end="", flush=True)
                else:
                    print("\rFailed to send temperature", end="", flush=True)
            else:
                print(f"\rSensor containing '{sensor_substring}' not found", end="", flush=True)
                # Send a keepalive to prevent display timeout
                current_time = time.time()
                if current_time - last_keepalive >= KEEPALIVE_INTERVAL:
                    controller.send_temperature(0, unit)  # Send 0 as keepalive
                    last_keepalive = current_time
            
            time.sleep(refresh_rate)
            
        except KeyboardInterrupt:
            print("\nStopping temperature display.")
            break
        except Exception as e:
            print(f"\nError in display loop: {e}")
            time.sleep(refresh_rate)


def select_and_read_sensor(sensor_substring: str = DEFAULT_SENSOR_SUBSTR) -> Optional[float]:
    """Selects and reads a temperature sensor."""
    sensors = get_temperature_sensors()
    sensor_data = find_sensor_by_substring(sensors, sensor_substring)
    
    if sensor_data:
        sensor_name, temp_celsius = sensor_data
        print(f"Using sensor: {sensor_name} ({temp_celsius:.1f}°C)")
        return temp_celsius
    else:
        print(f"Sensor containing '{sensor_substring}' not found.")
        print(build_temperature_report(sensor_substring))
        return None


def install_systemd_service(unit: str = 'c', 
                          sensor: str = DEFAULT_SENSOR_SUBSTR, 
                          rate: float = DEFAULT_REFRESH_RATE,
                          service_name: str = "ocypus-lcd"):
    """Creates and installs a systemd service unit."""
    script_path = os.path.abspath(__file__)
    
    service_content = f"""[Unit]
Description=Ocypus Iota A40 LCD Temperature Display
After=multi-user.target

[Service]
Type=simple
User=root
ExecStart={sys.executable} {script_path} on -u {unit} -s "{sensor}" -r {rate}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    
    service_file_path = f"/etc/systemd/system/{service_name}.service"
    
    try:
        with open(service_file_path, 'w') as f:
            f.write(service_content)
        
        print(f"Systemd service created: {service_file_path}")
        print("\nTo enable and start the service:")
        print(f"  sudo systemctl daemon-reload")
        print(f"  sudo systemctl enable --now {service_name}.service")
        print("\nTo check service status:")
        print(f"  systemctl status {service_name}.service")
        
    except PermissionError:
        print(f"Error: Permission denied. Run with sudo to install the service.")
    except Exception as e:
        print(f"Error creating service file: {e}")


def main():
    """Main function with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Ocypus Iota A40 LCD driver for Linux/Proxmox",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        Examples:
          %(prog)s list                    # List all Ocypus devices
          %(prog)s on                      # Start temperature display (Celsius)
          %(prog)s on -u f                 # Start temperature display (Fahrenheit)
          %(prog)s on -s "coretemp" -u c   # Use specific sensor
          %(prog)s off                     # Turn off display
          %(prog)s install-service         # Install systemd service
        """)
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # List command
    subparsers.add_parser('list', help='List all found Ocypus cooler devices')
    
    # On command
    on_parser = subparsers.add_parser('on', help='Turn on display and stream temperature')
    on_parser.add_argument('-u', '--unit', choices=['c', 'f'], default='c',
                          help='Temperature unit: c=Celsius, f=Fahrenheit (default: c)')
    on_parser.add_argument('-s', '--sensor', default=DEFAULT_SENSOR_SUBSTR,
                          help=f'Substring of psutil sensor to use (default: {DEFAULT_SENSOR_SUBSTR})')
    on_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_REFRESH_RATE,
                          help=f'Update interval in seconds (default: {DEFAULT_REFRESH_RATE})')
    
    # Off command
    subparsers.add_parser('off', help='Turn off (blank) the display')
    
    # Install service command
    service_parser = subparsers.add_parser('install-service', 
                                          help='Install systemd unit for background operation')
    service_parser.add_argument('-u', '--unit', choices=['c', 'f'], default='c',
                               help='Temperature unit for the service (default: c)')
    service_parser.add_argument('-s', '--sensor', default=DEFAULT_SENSOR_SUBSTR,
                               help=f'Sensor substring for the service (default: {DEFAULT_SENSOR_SUBSTR})')
    service_parser.add_argument('-r', '--rate', type=float, default=DEFAULT_REFRESH_RATE,
                               help=f'Update interval for the service (default: {DEFAULT_REFRESH_RATE})')
    service_parser.add_argument('--name', default='ocypus-lcd',
                               help='Name for the systemd unit file (default: ocypus-lcd)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Set up signal handling for graceful shutdown
    def signal_handler(signum: int, frame: Optional[FrameType]):
        print("\nReceived interrupt signal. Exiting gracefully...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    if args.command == 'list':
        controller = OcypusController()
        devices = controller.list_devices()
        if devices:
            print(f"Found {len(devices)} Ocypus cooler device(s):")
            for i, device in enumerate(devices, 1):
                interface = device.get('interface_number', 'Unknown')
                path = device.get('path', 'Unknown')
                print(f"  {i}. Interface {interface} (Path: {path.decode() if isinstance(path, bytes) else path})")
        else:
            print("No Ocypus cooler devices found.")
    
    elif args.command == 'on':
        with OcypusController() as controller:
            if controller.device:
                run_display_loop(controller, args.sensor, args.unit, args.rate)
    
    elif args.command == 'off':
        with OcypusController() as controller:
            if controller.device:
                success = controller.blank_display()
                if success:
                    print("Display turned off.")
                else:
                    print("Failed to turn off display.")
    
    elif args.command == 'install-service':
        install_systemd_service(args.unit, args.sensor, args.rate, args.name)


if __name__ == "__main__":
    main()
