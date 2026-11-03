#include "sht1x.h"
#include "esphome/core/log.h"

#include <algorithm>
#include <cmath>

namespace esphome::sht1x {

static const char *const TAG = "sht1x";

static const uint8_t CMD_TEMPERATURE = 0x03;  // 14 bit
static const uint8_t CMD_HUMIDITY = 0x05;     // 12 bit
static const uint32_t CLOCK_US = 10;          // slow and steady, for long probe cables
static const uint32_t POLL_MS = 20;
static const uint32_t MEASURE_TIMEOUT_MS = 500;  // the datasheet's longest is 320 ms
static const uint8_t FAILURES_BEFORE_UNKNOWN = 3;

// Datasheet conversion constants, for a 3.3 V supply.
static const float D1 = -39.66f;
static const float D2 = 0.01f;
static const float C1 = -2.0468f;
static const float C2 = 0.0367f;
static const float C3 = -1.5955e-6f;
static const float T1 = 0.01f;
static const float T2 = 0.00008f;

// CRC-8 (x^8 + x^5 + x^4 + 1) over the command and both data bytes, starting from the status
// register (0 by default). The sensor sends its CRC with the bits reversed.
static uint8_t crc8(uint8_t crc, uint8_t value) {
  crc ^= value;
  for (uint8_t i = 0; i < 8; i++)
    crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : crc << 1;
  return crc;
}

static uint8_t reverse_bits(uint8_t value) {
  uint8_t out = 0;
  for (uint8_t i = 0; i < 8; i++, value >>= 1)
    out = (out << 1) | (value & 1);
  return out;
}

void SHT1xComponent::setup() {
  this->clock_pin_->setup();
  this->clock_pin_->digital_write(false);
  this->clock_pin_->pin_mode(gpio::FLAG_OUTPUT);
  this->data_pin_->setup();
  this->release_data_();
}

void SHT1xComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "SHT1x:");
  LOG_PIN("  Data Pin: ", this->data_pin_);
  LOG_PIN("  Clock Pin: ", this->clock_pin_);
  LOG_UPDATE_INTERVAL(this);
  LOG_SENSOR("  ", "Temperature", this->temperature_sensor_);
  LOG_SENSOR("  ", "Humidity", this->humidity_sensor_);
}

void SHT1xComponent::update() {
  if (this->busy_) {
    ESP_LOGW(TAG, "The last reading hasn't finished yet");
    return;
  }
  this->busy_ = true;
  this->start_(CMD_TEMPERATURE);
}

void SHT1xComponent::start_(uint8_t command) {
  this->command_ = command;
  this->reset_connection_();
  this->transmission_start_();
  if (!this->send_byte_(command)) {
    this->fail_("no reply from the sensor (check the wiring and the pull-up on the data wire)");
    return;
  }
  this->started_ = millis();
  this->set_timeout("measure", POLL_MS, [this]() { this->wait_(); });
}

void SHT1xComponent::wait_() {
  if (this->data_pin_->digital_read()) {  // the sensor pulls data low when the reading is ready
    if (millis() - this->started_ > MEASURE_TIMEOUT_MS) {
      this->fail_("the sensor didn't finish the reading");
      return;
    }
    this->set_timeout("measure", POLL_MS, [this]() { this->wait_(); });
    return;
  }

  uint8_t msb = this->read_byte_(true);
  uint8_t lsb = this->read_byte_(true);
  uint8_t crc = this->read_byte_(false);
  if (crc8(crc8(crc8(0, this->command_), msb), lsb) != reverse_bits(crc)) {
    this->fail_("checksum mismatch (interference on the cable?)");
    return;
  }
  uint16_t raw = (uint16_t(msb) << 8) | lsb;

  if (this->command_ == CMD_TEMPERATURE) {
    this->raw_temperature_ = raw & 0x3FFF;
    this->start_(CMD_HUMIDITY);
    return;
  }

  float temperature = D1 + D2 * this->raw_temperature_;
  float so_rh = raw & 0x0FFF;
  float linear = C1 + C2 * so_rh + C3 * so_rh * so_rh;
  float humidity = std::clamp((temperature - 25.0f) * (T1 + T2 * so_rh) + linear, 0.0f, 100.0f);
  ESP_LOGD(TAG, "Temperature %.2f °C, humidity %.1f %%", temperature, humidity);

  this->busy_ = false;
  this->failures_ = 0;
  this->status_clear_warning();
  if (this->temperature_sensor_ != nullptr)
    this->temperature_sensor_->publish_state(temperature);
  if (this->humidity_sensor_ != nullptr)
    this->humidity_sensor_->publish_state(humidity);
}

void SHT1xComponent::fail_(const char *reason) {
  this->busy_ = false;
  this->release_data_();
  this->clock_pin_->digital_write(false);
  this->status_set_warning();
  ESP_LOGW(TAG, "Reading failed: %s", reason);
  // One bad reading is retried at the next update; a sensor that keeps failing shows as unknown.
  if (this->failures_ < FAILURES_BEFORE_UNKNOWN && ++this->failures_ == FAILURES_BEFORE_UNKNOWN) {
    if (this->temperature_sensor_ != nullptr)
      this->temperature_sensor_->publish_state(NAN);
    if (this->humidity_sensor_ != nullptr)
      this->humidity_sensor_->publish_state(NAN);
  }
}

// The data line is never driven high, only released, so the ESP32 and the sensor can't fight over it.
void SHT1xComponent::release_data_() { this->data_pin_->pin_mode(gpio::FLAG_INPUT | gpio::FLAG_PULLUP); }

void SHT1xComponent::pull_data_low_() {
  this->data_pin_->digital_write(false);
  this->data_pin_->pin_mode(gpio::FLAG_OUTPUT);
}

void SHT1xComponent::clock_(bool high) {
  this->clock_pin_->digital_write(high);
  delayMicroseconds(CLOCK_US);
}

// Nine clocks with data high: puts the sensor back in a known state after any error.
void SHT1xComponent::reset_connection_() {
  this->release_data_();
  delayMicroseconds(CLOCK_US);
  for (uint8_t i = 0; i < 9; i++) {
    this->clock_(true);
    this->clock_(false);
  }
}

void SHT1xComponent::transmission_start_() {
  this->release_data_();
  this->clock_(true);
  this->pull_data_low_();
  delayMicroseconds(CLOCK_US);
  this->clock_(false);
  this->clock_(true);
  this->release_data_();
  delayMicroseconds(CLOCK_US);
  this->clock_(false);
}

bool SHT1xComponent::send_byte_(uint8_t value) {
  for (uint8_t mask = 0x80; mask != 0; mask >>= 1) {
    if (value & mask) {
      this->release_data_();
    } else {
      this->pull_data_low_();
    }
    delayMicroseconds(CLOCK_US);
    this->clock_(true);
    this->clock_(false);
  }
  this->release_data_();
  delayMicroseconds(CLOCK_US);
  this->clock_pin_->digital_write(true);  // ninth clock: the sensor acknowledges by pulling data low
  delayMicroseconds(CLOCK_US);
  bool ack = !this->data_pin_->digital_read();
  this->clock_(false);
  return ack;
}

uint8_t SHT1xComponent::read_byte_(bool more) {
  this->release_data_();
  uint8_t value = 0;
  for (uint8_t i = 0; i < 8; i++) {
    this->clock_(true);
    value = (value << 1) | (this->data_pin_->digital_read() ? 1 : 0);
    this->clock_(false);
  }
  if (more)
    this->pull_data_low_();  // acknowledge, so the sensor sends the next byte
  delayMicroseconds(CLOCK_US);
  this->clock_(true);
  this->clock_(false);
  this->release_data_();
  return value;
}

}  // namespace esphome::sht1x
