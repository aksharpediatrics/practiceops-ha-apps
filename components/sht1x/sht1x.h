#pragma once

#include "esphome/core/component.h"
#include "esphome/core/hal.h"
#include "esphome/components/sensor/sensor.h"

namespace esphome::sht1x {

/// Sensirion SHT1x (SHT10/11/15) over its own two-wire protocol (not I2C): a clock pin the ESP32
/// drives, and a data pin that is only ever pulled low or released (a pull-up to 3.3 V makes it high).
/// A measurement takes up to about 320 ms, so it is waited on with timers instead of blocking.
class SHT1xComponent : public PollingComponent {
 public:
  void set_data_pin(InternalGPIOPin *pin) { this->data_pin_ = pin; }
  void set_clock_pin(InternalGPIOPin *pin) { this->clock_pin_ = pin; }
  void set_temperature_sensor(sensor::Sensor *sensor) { this->temperature_sensor_ = sensor; }
  void set_humidity_sensor(sensor::Sensor *sensor) { this->humidity_sensor_ = sensor; }

  void setup() override;
  void dump_config() override;
  void update() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

 protected:
  void start_(uint8_t command);
  void wait_();
  void fail_(const char *reason);

  void release_data_();
  void pull_data_low_();
  void clock_(bool high);
  void reset_connection_();
  void transmission_start_();
  bool send_byte_(uint8_t value);
  uint8_t read_byte_(bool more);

  InternalGPIOPin *data_pin_;
  InternalGPIOPin *clock_pin_;
  sensor::Sensor *temperature_sensor_{nullptr};
  sensor::Sensor *humidity_sensor_{nullptr};

  bool busy_{false};
  uint8_t command_{0};
  uint32_t started_{0};
  uint16_t raw_temperature_{0};
  uint8_t failures_{0};
};

}  // namespace esphome::sht1x
