////////////////////////////////////////////////////
// COHI-Player-Universal
// V1.0.0, 24.10.2026
// Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
////////////////////////////////////////////////////

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <pthread.h>
#include <sys/socket.h>
#include <unistd.h>

static constexpr float PEAK_HOLD_DECAY_PER_SAMPLE = 0.9999886622f;

struct LiveChannel {
    int udp_port = 0;
    int sock = -1;

    static constexpr uint32_t RING_SIZE = 1048576;
    std::vector<uint8_t> ring;
    std::atomic<uint32_t> head{0};
    std::atomic<uint32_t> tail{0};

    double samples_per_audio_sample = 1.0;
    double audio_phase = 0.0;

    uint64_t starved_count = 0;
    std::atomic<uint64_t> overflow_count{0};

    float gain = 1.0f;
    float peak_hold = 0.1f;

    double freq_hz = 0.0;
    std::atomic<float> external_gain{1.0f};
};

struct LiveCarrierState {
    std::vector<float> cos_val;
    std::vector<float> sin_val;
    std::vector<float> cos_delta;
    std::vector<float> sin_delta;
    std::vector<float> mod_register;
    std::vector<float> mod_prev;
    std::vector<float> mod_next;

    void resize(size_t n) {
        cos_val.assign(n, 1.0f);
        sin_val.assign(n, 0.0f);
        cos_delta.assign(n, 1.0f);
        sin_delta.assign(n, 0.0f);
        mod_register.assign(n, 1.0f);
        mod_prev.assign(n, 1.0f);
        mod_next.assign(n, 1.0f);
    }

    void set_frequency(size_t i, double freq_offset_hz, double iq_sample_rate) {
        double angle = 2.0 * M_PI * freq_offset_hz / iq_sample_rate;
        cos_delta[i] = static_cast<float>(std::cos(angle));
        sin_delta[i] = static_cast<float>(std::sin(angle));
    }

    void renormalize() {
        for (size_t i = 0; i < cos_val.size(); ++i) {
            float mag_sq = cos_val[i] * cos_val[i] + sin_val[i] * sin_val[i];
            float correction = 1.5f - 0.5f * mag_sq;
            cos_val[i] *= correction;
            sin_val[i] *= correction;
        }
    }
};

// Steuerport im Protokoll des AMWaveSynth / AMWaveSynthPropagationSimulator:
// UDP-Datagramm als ASCII-Text "FREQ_HZ:GAIN" (z.B. "603000:0.35"). Der Kanal
// wird ueber die Traegerfrequenz gefunden (Toleranz < 1 Hz), GAIN skaliert
// Traeger und Modulation des Senders gemeinsam (1.0 = unveraendert, 0 = aus).
struct LiveControlArgs {
    std::vector<LiveChannel>* channels;
    int port;
    const char* tool_name;
};

void* live_control_receiver(void* arg) {
    LiveControlArgs* ca = static_cast<LiveControlArgs*>(arg);
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        std::fprintf(stderr, "%s: Steuerport %d: Socket konnte nicht angelegt werden\n",
                     ca->tool_name, ca->port);
        return nullptr;
    }
    int reuse = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(ca->port));
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sock, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::fprintf(stderr, "%s: Steuerport UDP %d: bind() fehlgeschlagen - "
                     "Sendersteuerung (Feldstaerke) nicht verfuegbar\n", ca->tool_name, ca->port);
        close(sock);
        return nullptr;
    }
    std::fprintf(stderr, "%s: Steuerport UDP %d bereit (Format FREQ_HZ:GAIN)\n",
                 ca->tool_name, ca->port);

    char buf[256];
    bool first_message_logged = false;
    while (true) {
        ssize_t n = recv(sock, buf, sizeof(buf) - 1, 0);
        if (n <= 0) {
            usleep(1000);
            continue;
        }
        buf[n] = '\0';
        double freq = 0.0, gain = 0.0;
        if (std::sscanf(buf, "%lf:%lf", &freq, &gain) != 2) continue;
        if (!std::isfinite(gain)) continue;
        if (gain < 0.0) gain = 0.0;
        for (LiveChannel& ch : *ca->channels) {
            if (std::fabs(ch.freq_hz - freq) < 1.0) {
                ch.external_gain.store(static_cast<float>(gain), std::memory_order_relaxed);
                if (!first_message_logged) {
                    std::fprintf(stderr, "%s: erster Steuerbefehl: %.0f Hz -> Gain %.3f\n",
                                 ca->tool_name, ch.freq_hz, gain);
                    first_message_logged = true;
                }
                break;
            }
        }
    }
    return nullptr;
}

// Sferics / Blitzsimulation im Protokoll des AMWaveSynth: UDP-Datagramm
// "AMPLITUDE:DAUER_MS" (z.B. "800:12.5"). Ein Blitz ist ein Rauschburst mit
// exponentiell abklingender Huellkurve (Abfall auf 1 % nach DAUER_MS). Die
// Amplitude ist wie im Original relativ zum Traeger (AMPLITUDE/128 = Spitzenwert
// des Rauschens im Verhaeltnis zu einem Traeger mit Gain 1.0).
std::atomic<float> g_sferics_decay{0.0f};
std::atomic<float> g_sferics_amp{0.0f};   // wird zuletzt geschrieben = "Go"-Signal

struct SfericsArgs {
    int port;
    double rate;
    const char* tool_name;
};

void* sferics_receiver(void* arg) {
    SfericsArgs* sa = static_cast<SfericsArgs*>(arg);
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        std::fprintf(stderr, "%s: Sferics-Port %d: Socket konnte nicht angelegt werden\n",
                     sa->tool_name, sa->port);
        return nullptr;
    }
    int reuse = 1;
    setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
    struct sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(sa->port));
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(sock, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::fprintf(stderr, "%s: Sferics-Port UDP %d: bind() fehlgeschlagen - "
                     "Blitzsimulation nicht verfuegbar\n", sa->tool_name, sa->port);
        close(sock);
        return nullptr;
    }
    std::fprintf(stderr, "%s: Sferics-Port UDP %d bereit (Format AMPLITUDE:DAUER_MS)\n",
                 sa->tool_name, sa->port);

    char buf[256];
    while (true) {
        ssize_t n = recv(sock, buf, sizeof(buf) - 1, 0);
        if (n <= 0) {
            usleep(1000);
            continue;
        }
        buf[n] = '\0';
        double amp = 0.0, decay_ms = 0.0;
        if (std::sscanf(buf, "%lf:%lf", &amp, &decay_ms) != 2) continue;
        if (!std::isfinite(amp) || !std::isfinite(decay_ms) || amp <= 0.0 || decay_ms < 0.0) continue;
        double total_samples = (decay_ms / 1000.0) * sa->rate;
        float decay = static_cast<float>(std::exp(-4.60517 / (total_samples + 1.0)));
        g_sferics_decay.store(decay, std::memory_order_relaxed);
        g_sferics_amp.store(static_cast<float>(amp), std::memory_order_release);
    }
    return nullptr;
}

struct SfericsState {
    float env = 0.0f;
    float amp = 0.0f;
    float decay = 0.0f;
    uint32_t prng = 2463534242u;

    // einmal pro Batch aufrufen: neuen Blitz uebernehmen
    void poll() {
        float trig = g_sferics_amp.load(std::memory_order_acquire);
        if (trig > 0.0f) {
            decay = g_sferics_decay.load(std::memory_order_relaxed);
            amp = trig;
            env = 1.0f;
            g_sferics_amp.store(0.0f, std::memory_order_relaxed);
        }
    }
    // Rauschwert -1..1 (Xorshift32)
    float noise() {
        prng ^= prng << 13;
        prng ^= prng >> 17;
        prng ^= prng << 5;
        return (static_cast<float>(prng & 0xFFFF) - 32768.0f) * (1.0f / 32768.0f);
    }
    void step_env() {
        env *= decay;
        if (env < 3e-4f) env = 0.0f;
    }
};

void* live_channel_receiver(void* arg) {
    LiveChannel* ch = static_cast<LiveChannel*>(arg);
    uint8_t buf[4096];
    while (true) {
        ssize_t n = recv(ch->sock, buf, sizeof(buf), 0);
        if (n > 0) {
            uint32_t head = ch->head.load(std::memory_order_relaxed);
            uint32_t tail = ch->tail.load(std::memory_order_acquire);
            uint32_t used = head - tail;
            uint32_t free_space = LiveChannel::RING_SIZE - used;
            size_t to_write = std::min(static_cast<size_t>(n), static_cast<size_t>(free_space));

            if (to_write < static_cast<size_t>(n)) {
                ch->overflow_count.fetch_add(static_cast<uint64_t>(n) - to_write,
                                              std::memory_order_relaxed);
            }

            if (to_write > 0) {
                uint32_t start_idx = head & (LiveChannel::RING_SIZE - 1);
                size_t first_part = std::min(to_write,
                                              static_cast<size_t>(LiveChannel::RING_SIZE - start_idx));
                std::memcpy(&ch->ring[start_idx], buf, first_part);
                if (first_part < to_write) {
                    std::memcpy(&ch->ring[0], buf + first_part, to_write - first_part);
                }
                ch->head.store(head + static_cast<uint32_t>(to_write), std::memory_order_release);
            }
        } else {
            usleep(1000);
        }
    }
    return nullptr;
}

struct LiveChannelArg {
    int udp_port = 0;
    double freq_hz = 0.0;
    double audio_rate_hz = 0.0;
};

struct LiveSetupResult {
    std::vector<LiveChannel> channels;
    LiveCarrierState carrier;
};

LiveSetupResult setup_live_channels(const std::vector<LiveChannelArg>& specs,
                                     double lo_freq, double iq_sample_rate) {
    struct Prepared {
        LiveChannelArg spec;
        int sock;
    };
    std::vector<Prepared> prepared;
    for (const auto& spec : specs) {
        int sock = socket(AF_INET, SOCK_DGRAM, 0);
        if (sock < 0) {
            std::fprintf(stderr, "cohi_stemlab_live_upconverter: Live-Kanal Port %d: "
                         "Socket konnte nicht angelegt werden\n", spec.udp_port);
            continue;
        }
        struct sockaddr_in addr;
        std::memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<uint16_t>(spec.udp_port));
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (bind(sock, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) != 0) {
            std::fprintf(stderr, "cohi_stemlab_live_upconverter: Live-Kanal Port %d: "
                         "bind() fehlgeschlagen, Kanal bleibt stumm\n", spec.udp_port);
            close(sock);
            continue;
        }
        prepared.push_back({spec, sock});
    }

    LiveSetupResult result;
    result.channels = std::vector<LiveChannel>(prepared.size());
    result.carrier.resize(prepared.size());
    for (size_t i = 0; i < prepared.size(); ++i) {
        LiveChannel& ch = result.channels[i];
        ch.ring.resize(LiveChannel::RING_SIZE);
        ch.udp_port = prepared[i].spec.udp_port;
        ch.sock = prepared[i].sock;
        ch.freq_hz = prepared[i].spec.freq_hz;
        ch.samples_per_audio_sample = iq_sample_rate / prepared[i].spec.audio_rate_hz;

        result.carrier.set_frequency(i, prepared[i].spec.freq_hz - lo_freq, iq_sample_rate);

        pthread_t tid;
        pthread_create(&tid, nullptr, live_channel_receiver, &ch);
        pthread_detach(tid);

        std::fprintf(stderr, "cohi_stemlab_live_upconverter: Live-Kanal auf UDP-Port %d bei "
                     "%.0f Hz (LO-Offset %.0f Hz, Audiorate %.0f Hz)\n",
                     prepared[i].spec.udp_port, prepared[i].spec.freq_hz,
                     prepared[i].spec.freq_hz - lo_freq, prepared[i].spec.audio_rate_hz);
    }
    return result;
}

struct Args {
    double lo_freq = 0.0;
    double iq_sample_rate = 0.0;
    double modulation_depth = 0.8;
    double live_gain = 1.0;
    int live_control_port = 0;
    int live_sferics_port = 0;
    std::vector<LiveChannelArg> live_channels;
};

bool parse_live_channel_arg(const std::string& value, LiveChannelArg& out) {
    size_t first_colon = value.find(':');
    size_t second_colon = value.find(':', first_colon + 1);
    if (first_colon == std::string::npos || second_colon == std::string::npos) {
        std::fprintf(stderr, "cohi_stemlab_live_upconverter: ungueltiges --live-channel Format "
                     "(erwartet PORT:FREQ_HZ:AUDIO_RATE): %s\n", value.c_str());
        return false;
    }
    try {
        out.udp_port = std::stoi(value.substr(0, first_colon));
        out.freq_hz = std::stod(value.substr(first_colon + 1, second_colon - first_colon - 1));
        out.audio_rate_hz = std::stod(value.substr(second_colon + 1));
    } catch (const std::exception&) {
        std::fprintf(stderr, "cohi_stemlab_live_upconverter: ungueltiges --live-channel Format "
                     "(Zahl erwartet): %s\n", value.c_str());
        return false;
    }
    return true;
}

bool parse_args(int argc, char** argv, Args& out) {
    bool have_lo = false, have_rate = false;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        auto next_value = [&](const char* flag_name) -> const char* {
            if (i + 1 >= argc) {
                std::fprintf(stderr, "cohi_stemlab_live_upconverter: %s benoetigt einen Wert\n", flag_name);
                return nullptr;
            }
            return argv[++i];
        };
        if (arg == "--lo-freq") {
            const char* v = next_value("--lo-freq");
            if (!v) return false;
            out.lo_freq = std::stod(v);
            have_lo = true;
        } else if (arg == "--iq-sample-rate") {
            const char* v = next_value("--iq-sample-rate");
            if (!v) return false;
            out.iq_sample_rate = std::stod(v);
            have_rate = true;
        } else if (arg == "--modulation-depth") {
            const char* v = next_value("--modulation-depth");
            if (!v) return false;
            out.modulation_depth = std::stod(v);
        } else if (arg == "--live-gain") {
            const char* v = next_value("--live-gain");
            if (!v) return false;
            out.live_gain = std::stod(v);
        } else if (arg == "--live-control-port") {
            const char* v = next_value("--live-control-port");
            if (!v) return false;
            out.live_control_port = std::atoi(v);
        } else if (arg == "--live-sferics-port") {
            const char* v = next_value("--live-sferics-port");
            if (!v) return false;
            out.live_sferics_port = std::atoi(v);
        } else if (arg == "--live-channel") {
            const char* v = next_value("--live-channel");
            if (!v) return false;
            LiveChannelArg ch;
            if (!parse_live_channel_arg(v, ch)) return false;
            out.live_channels.push_back(ch);
        } else {
            std::fprintf(stderr, "cohi_stemlab_live_upconverter: unbekannte Option: %s\n", arg.c_str());
            return false;
        }
    }
    if (!have_lo || !have_rate) {
        std::fprintf(stderr, "cohi_stemlab_live_upconverter: --lo-freq und --iq-sample-rate "
                     "sind Pflichtangaben\n");
        return false;
    }
    if (out.live_channels.empty()) {
        std::fprintf(stderr, "cohi_stemlab_live_upconverter: mindestens ein --live-channel "
                     "ist erforderlich (dieses Tool ist reiner Live-Kanal-Mischer, kein "
                     "WAV-Pfad - siehe Datei-Kommentar oben)\n");
        return false;
    }
    return true;
}

int main(int argc, char** argv) {
    Args args;
    if (!parse_args(argc, argv, args)) {
        return 1;
    }

    LiveSetupResult setup = setup_live_channels(args.live_channels, args.lo_freq, args.iq_sample_rate);
    std::vector<LiveChannel>& live_channels = setup.channels;
    LiveCarrierState& carrier = setup.carrier;
    const size_t n_live_channels = live_channels.size();

    if (n_live_channels == 0) {
        std::fprintf(stderr, "cohi_stemlab_live_upconverter: kein einziger Live-Kanal konnte "
                     "eingerichtet werden, breche ab\n");
        return 1;
    }

    LiveControlArgs live_control_args{&live_channels, args.live_control_port,
                                      "cohi_stemlab_live_upconverter"};
    if (args.live_control_port > 0) {
        pthread_t control_tid;
        pthread_create(&control_tid, nullptr, live_control_receiver, &live_control_args);
        pthread_detach(control_tid);
    }

    SfericsArgs sferics_args{args.live_sferics_port, args.iq_sample_rate,
                             "cohi_stemlab_live_upconverter"};
    if (args.live_sferics_port > 0) {
        pthread_t sferics_tid;
        pthread_create(&sferics_tid, nullptr, sferics_receiver, &sferics_args);
        pthread_detach(sferics_tid);
    }
    SfericsState sferics;

    const size_t OUTPUT_BATCH_SIZE = 4096;
    std::vector<float> out_buf(OUTPUT_BATCH_SIZE * 2);

    const auto stream_started_at = std::chrono::steady_clock::now();
    uint64_t samples_emitted = 0;

    auto load_window_started_at = stream_started_at;
    double busy_seconds_in_window = 0.0;
    double audio_seconds_in_window = 0.0;
    double max_batch_seconds_in_window = 0.0;
    constexpr double LOAD_LOG_INTERVAL_S = 10.0;

    while (true) {
        auto batch_started_at = std::chrono::steady_clock::now();
        sferics.poll();

        for (size_t out_idx = 0; out_idx < OUTPUT_BATCH_SIZE; ++out_idx) {
            for (size_t i = 0; i < n_live_channels; ++i) {
                LiveChannel& ch = live_channels[i];
                ch.audio_phase += 1.0;
                if (ch.audio_phase >= ch.samples_per_audio_sample) {
                    ch.audio_phase -= ch.samples_per_audio_sample;
                    carrier.mod_prev[i] = carrier.mod_next[i];

                    uint32_t lhead = ch.head.load(std::memory_order_acquire);
                    uint32_t ltail = ch.tail.load(std::memory_order_relaxed);
                    if (lhead == ltail) {
                        ++ch.starved_count;
                    } else {
                        int32_t raw = static_cast<int32_t>(
                            ch.ring[ltail & (LiveChannel::RING_SIZE - 1)]) - 128;
                        ch.tail.store(ltail + 1, std::memory_order_release);

                        float raw_f = static_cast<float>(raw) / 128.0f;
                        float peak = std::fabs(raw_f);
                        ch.peak_hold *= PEAK_HOLD_DECAY_PER_SAMPLE;
                        if (peak > ch.peak_hold) {
                            ch.peak_hold = 0.995f * ch.peak_hold + 0.005f * peak;
                        }
                        float target_gain = 0.8f / (ch.peak_hold + 0.001f);
                        ch.gain = 0.998f * ch.gain + 0.002f * target_gain;
                        if (ch.gain > 8.0f) ch.gain = 8.0f;
                        if (ch.gain < 0.1f) ch.gain = 0.1f;

                        carrier.mod_next[i] = 1.0f + static_cast<float>(args.modulation_depth)
                                                    * ch.gain * raw_f;
                    }
                }
                float frac = static_cast<float>(ch.audio_phase / ch.samples_per_audio_sample);
                carrier.mod_register[i] = (carrier.mod_prev[i]
                                          + frac * (carrier.mod_next[i] - carrier.mod_prev[i]))
                                         * ch.external_gain.load(std::memory_order_relaxed);
            }

            float i_sum = 0.0f, q_sum = 0.0f;
            for (size_t i = 0; i < n_live_channels; ++i) {
                float new_cos = carrier.cos_val[i] * carrier.cos_delta[i]
                               - carrier.sin_val[i] * carrier.sin_delta[i];
                float new_sin = carrier.sin_val[i] * carrier.cos_delta[i]
                               + carrier.cos_val[i] * carrier.sin_delta[i];
                carrier.cos_val[i] = new_cos;
                carrier.sin_val[i] = new_sin;
                i_sum += carrier.mod_register[i] * new_cos;
                q_sum += carrier.mod_register[i] * new_sin;
            }
            float norm = static_cast<float>(n_live_channels);
            float gain_f = static_cast<float>(args.live_gain);
            if (sferics.env > 0.0f) {
                // komplexes Rauschen (unabhaengig auf I und Q), Amplitude relativ zum Traeger
                float burst_scale = sferics.env * sferics.amp * (1.0f / 128.0f);
                i_sum += sferics.noise() * burst_scale;
                q_sum += sferics.noise() * burst_scale;
                sferics.step_env();
            }
            out_buf[2 * out_idx]     = (i_sum / norm) * gain_f;
            out_buf[2 * out_idx + 1] = (q_sum / norm) * gain_f;
        }

        carrier.renormalize();

        auto compute_ended_at = std::chrono::steady_clock::now();
        double this_batch_seconds = std::chrono::duration<double>(compute_ended_at - batch_started_at).count();
        busy_seconds_in_window += this_batch_seconds;
        if (this_batch_seconds > max_batch_seconds_in_window) {
            max_batch_seconds_in_window = this_batch_seconds;
        }
        audio_seconds_in_window += static_cast<double>(OUTPUT_BATCH_SIZE) / args.iq_sample_rate;

        samples_emitted += OUTPUT_BATCH_SIZE;
        auto due_at = stream_started_at + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
            std::chrono::duration<double>(static_cast<double>(samples_emitted) / args.iq_sample_rate));
        std::this_thread::sleep_until(due_at);

        std::fwrite(out_buf.data(), sizeof(float), OUTPUT_BATCH_SIZE * 2, stdout);
        std::fflush(stdout);

        auto window_check_at = std::chrono::steady_clock::now();
        double elapsed_in_window = std::chrono::duration<double>(window_check_at - load_window_started_at).count();
        if (elapsed_in_window >= LOAD_LOG_INTERVAL_S) {
            double load_percent = (audio_seconds_in_window > 0.0)
                ? (busy_seconds_in_window / audio_seconds_in_window) * 100.0 : 0.0;

            uint64_t total_starved = 0;
            std::string starved_detail;
            uint64_t total_overflow = 0;
            std::string overflow_detail;
            for (LiveChannel& ch : live_channels) {
                total_starved += ch.starved_count;
                if (ch.starved_count > 0) {
                    if (!starved_detail.empty()) starved_detail += ", ";
                    starved_detail += std::to_string(ch.udp_port) + ":"
                                     + std::to_string(ch.starved_count);
                }
                ch.starved_count = 0;

                uint64_t overflow = ch.overflow_count.exchange(0, std::memory_order_relaxed);
                total_overflow += overflow;
                if (overflow > 0) {
                    if (!overflow_detail.empty()) overflow_detail += ", ";
                    overflow_detail += std::to_string(ch.udp_port) + ":" + std::to_string(overflow);
                }
            }
            if (starved_detail.empty()) starved_detail = "keine";
            if (overflow_detail.empty()) overflow_detail = "keine";

            std::fprintf(stderr,
                "cohi_stemlab_live_upconverter: Auslastung %.1f%% (%.3fs Rechenzeit fuer %.3fs "
                "Audio in den letzten %.0fs, %zu Live-Kanaele, groesster Batch %.1fms, "
                "hungrig gesamt: %llu, betroffene Ports (Port:Anzahl): %s, Ueberlauf gesamt: "
                "%llu, betroffene Ports (Port:Anzahl): %s)\n",
                load_percent, busy_seconds_in_window, audio_seconds_in_window,
                elapsed_in_window, n_live_channels, max_batch_seconds_in_window * 1000.0,
                static_cast<unsigned long long>(total_starved), starved_detail.c_str(),
                static_cast<unsigned long long>(total_overflow), overflow_detail.c_str());

            load_window_started_at = window_check_at;
            busy_seconds_in_window = 0.0;
            audio_seconds_in_window = 0.0;
            max_batch_seconds_in_window = 0.0;
        }
    }

    return 0;
}
