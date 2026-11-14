#include "fack-model.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace fack1996 {
Controller::Controller(Config config, Observer observer)
    : m_config(config), m_observer(std::move(observer)) {
    if (!std::isfinite(config.initialCwnd) || !std::isfinite(config.initialSsthresh) ||
        config.initialCwnd < 1 || config.initialSsthresh < 2 || !config.receiverWindow)
        throw std::invalid_argument("invalid window configuration");
    if (config.algorithm != Algorithm::Fack && (config.overdamping || config.rampdown))
        throw std::invalid_argument("Overdamping/Rampdown require FACK");
    m_state.cwnd = config.initialCwnd;
    m_state.ssthresh = config.initialSsthresh;
    m_applicationEnd = config.totalSegments ? config.totalSegments
        : config.saturated ? std::numeric_limits<uint32_t>::max() : 0;
}
void Controller::Grow(uint32_t size) { if (m_segments.size() < size) m_segments.resize(size); }
void Controller::Emit(const std::string& event, int64_t sequence) const {
    CheckInvariants();
    if (m_observer) m_observer(event, m_state, sequence);
}
double Controller::Outstanding() const {
    if (m_config.algorithm == Algorithm::Reno) return m_state.nxt - m_state.una;
    if (m_config.algorithm == Algorithm::RenoSack)
        return m_state.recovery ? m_state.sackPipe : m_state.nxt - m_state.una;
    return m_state.nxt - m_state.fack + m_state.retransmittedOutstanding;
}
double Controller::EffectiveWindow() const {
    if (m_config.algorithm == Algorithm::Reno)
        return std::min(double(m_config.receiverWindow),
                        m_state.cwnd + m_state.renoInflation);
    if (m_config.algorithm == Algorithm::RenoSack)
        return std::min(double(m_config.receiverWindow), m_state.cwnd);
    return m_state.cwnd + m_state.wintrim;
}
void Controller::CheckInvariants() const {
    const auto& s = m_state;
    if (!(s.una <= s.fack && s.fack <= s.highWater && s.una <= s.nxt && s.nxt <= s.highWater) ||
        (m_config.algorithm == Algorithm::Fack && s.fack > s.nxt) ||
        !std::isfinite(s.cwnd) || s.cwnd < 1 || s.wintrim < 0)
        throw std::logic_error("invalid sequence/window ordering");
    uint32_t count = 0;
    for (uint32_t i : m_pendingCopies) {
        if (!m_segments[i].retransmitted) throw std::logic_error("pending ledger flag missing");
        if (i < s.una || m_segments[i].sacked)
            throw std::logic_error("ACKed/SACKed segment still counted in retran_data");
        ++count;
    }
    if (count != s.retransmittedOutstanding)
        throw std::logic_error("retran_data differs from retransmission ledger");
    for (uint32_t i:m_restartCopies)
        if (i<s.una || i<s.fack || i>=s.nxt || m_segments[i].sacked || m_pendingCopies.count(i))
            throw std::logic_error("restart copy is not represented exactly once");
}
void Controller::Send(Result& r, uint32_t sequence, bool selective) {
    auto& s = m_state;
    Grow(sequence + 1);
    bool retransmission = sequence < s.highWater;
    if (retransmission) m_segments[sequence].wasRetransmitted = true;
    if (selective) {
        if (m_segments[sequence].sacked || m_segments[sequence].retransmitted)
            throw std::logic_error("attempt to retransmit received/pending segment");
        m_segments[sequence].retransmitted = true;

        m_segments[sequence].nxtAtRetransmit = s.highWater;
        ++s.retransmittedOutstanding;
        m_pendingCopies.insert(sequence);
    } else {
        if (retransmission && m_config.algorithm == Algorithm::Fack) {
            m_restartCopies.insert(sequence);
            m_segments[sequence].nxtAtRetransmit = s.highWater;
        }
        ++s.nxt;
        s.highWater = std::max(s.highWater, s.nxt);
    }
    r.transmissions.push_back({sequence, retransmission});
    Emit(selective ? "selective-retransmit" : retransmission ? "restart-retransmit" : "send-new",
         sequence);
}
void Controller::Pump(Result& r) {
    auto& s = m_state;
    const double limit = m_config.packetWindow ? std::floor(EffectiveWindow()) : EffectiveWindow();
    while (Outstanding() < limit) {
        bool found = false;
        if (m_config.algorithm != Algorithm::Reno && s.recovery) {
            for (uint32_t i = s.una; i < s.fack; ++i) {
                if (m_segments[i].sacked || m_segments[i].retransmitted) continue;
                if (m_config.algorithm == Algorithm::RenoSack) ++s.sackPipe;
                Send(r, i, true);
                found = true;
                break;
            }
        }
        if (found) continue;
        if (s.nxt >= s.una + m_config.receiverWindow ||
            s.nxt >= m_applicationEnd) break;
        if (m_config.algorithm == Algorithm::RenoSack && s.recovery) ++s.sackPipe;
        Send(r, s.nxt, false);
    }
}
void Controller::IncreaseWindow() {
    m_state.cwnd += m_state.cwnd < m_state.ssthresh ? 1.0 : 1.0 / m_state.cwnd;
}
double Controller::CutWindow(double multiplier) const {

    double reduced = std::min(m_state.cwnd, double(m_config.receiverWindow)) * multiplier;
    if (m_config.integerCut) reduced = std::floor(reduced);
    return std::max(1.0, reduced);
}
Result Controller::Start() { Result r; Emit("start"); Pump(r); return r; }
Result Controller::OfferSegments(uint32_t additional) {
    if (additional > std::numeric_limits<uint32_t>::max() - m_applicationEnd)
        throw std::invalid_argument("application sequence overflow");
    m_applicationEnd += additional;
    Result r; Emit("application-offer"); Pump(r); return r;
}
Result Controller::Receive(const Ack& ack) {
    if (ack.cumulative > m_state.highWater) throw std::invalid_argument("ACK of unsent data");
    for (auto [l, h] : ack.sacks)
        if (l >= h || h > m_state.highWater || l < ack.cumulative)
            throw std::invalid_argument("invalid SACK block");
    if (ack.cumulative < m_state.una) { Emit("stale-ack"); return {}; }
    if (m_config.algorithm == Algorithm::Fack) return ReceiveFack(ack);
    if (m_config.algorithm == Algorithm::RenoSack) return ReceiveSack(ack);
    return ReceiveReno(ack);
}
Result Controller::ReceiveFack(const Ack& ack) {
    auto& s = m_state;
    Result r;
    r.newAck = ack.cumulative > s.una;
    const uint32_t previousFack = s.fack;
    const bool wasRecovery = s.recovery;
    auto markReceived = [&](uint32_t i, bool sack) {
        auto& item = m_segments[i];
        m_restartCopies.erase(i);
        if (item.retransmitted) { item.retransmitted = false; --s.retransmittedOutstanding; m_pendingCopies.erase(i); }
        if (sack) item.sacked = true;
    };
    for (uint32_t i = s.una; i < ack.cumulative; ++i) markReceived(i, false);
    s.una = ack.cumulative;
    s.fack = std::max(s.fack, s.una);
    for (auto [left, right] : ack.sacks) {
        for (uint32_t i = left; i < right; ++i) markReceived(i, true);
        s.fack = std::max(s.fack, right);
    }

    s.nxt = std::max(s.nxt, s.fack);

    for (auto it=m_restartCopies.begin();it!=m_restartCopies.end();) {
        uint32_t i=*it;
        if (i>=s.fack) { ++it; continue; }
        m_segments[i].retransmitted=true;
        m_pendingCopies.insert(i);
        ++s.retransmittedOutstanding;
        it=m_restartCopies.erase(it);
    }
    if (r.newAck) s.duplicateAcks = 0;
    else if (s.una < s.highWater) ++s.duplicateAcks;

    for (uint32_t i = s.una; i < s.fack; ++i)
        if (m_segments[i].retransmitted && s.fack > m_segments[i].nxtAtRetransmit)
            r.forcedTimeout = true;
    if (r.forcedTimeout) { Emit("lost-retransmission"); return r; }

    if (wasRecovery && m_config.rampdown)
        s.wintrim = std::max(0.0, s.wintrim - (s.fack - previousFack) * (1 - s.winmult));
    if (wasRecovery && s.una >= s.recoverPoint) {
        s.recovery = false;
        s.wintrim = 0;
        r.exitedRecovery = true;
        Emit("recovery-exit");
    } else if (!wasRecovery && r.newAck) IncreaseWindow();
    if (!s.recovery && !r.exitedRecovery &&
        (s.fack - s.una > 3 || s.duplicateAcks == 3)) {
        s.winmult = 0.5;

        if (m_config.overdamping && s.cwnd <= s.ssthresh + 0.5) s.winmult = 0.25;

        s.cwnd = CutWindow(s.winmult);
        s.ssthresh = std::max(2.0, s.cwnd);
        s.recovery = true;
        s.recoverPoint = s.highWater;
        s.wintrim = m_config.rampdown ? (s.nxt - s.fack) * (1 - s.winmult) : 0;
        r.enteredRecovery = true;
        Emit("recovery-enter");
    }
    Emit("ack", ack.cumulative);

    if (r.newAck || s.recovery || s.duplicateAcks == 0) Pump(r);
    return r;
}
Result Controller::ReceiveReno(const Ack& ack) {
    auto& s = m_state;
    Result r;
    r.newAck = ack.cumulative > s.una;
    if (r.newAck) {
        s.una = ack.cumulative;
        s.fack = s.una;
        s.nxt = std::max(s.nxt, s.una);
        s.duplicateAcks = s.renoInflation = 0;
        r.exitedRecovery = s.recovery;
        s.recovery = false;
        IncreaseWindow();
    } else if (s.nxt > s.una) {
        ++s.duplicateAcks;
        if (s.duplicateAcks == 3) {
            if (s.una >= m_renoHoldUntil) {
                s.cwnd = CutWindow(.5);
                s.ssthresh = std::max(2.0, s.cwnd);
                s.recovery = true;
                s.recoverPoint = s.nxt;
                r.enteredRecovery = true;
                m_renoHoldUntil = 0;
                r.transmissions.push_back({s.una, true});
                Emit("reno-fast-retransmit", s.una);
            }
            s.renoInflation = 3;
        } else if (s.duplicateAcks > 3) ++s.renoInflation;
    }
    Emit("ack", ack.cumulative);
    if (s.duplicateAcks == 0 || s.duplicateAcks >= 3) Pump(r);
    return r;
}
Result Controller::ReceiveSack(const Ack& ack) {
    auto& s = m_state;
    Result r;
    r.newAck = ack.cumulative > s.una;
    bool repairedHole = false;
    for (uint32_t i = s.una; i < ack.cumulative; ++i) {
        if (m_segments[i].retransmitted) {
            repairedHole = true; m_segments[i].retransmitted = false;
            --s.retransmittedOutstanding; m_pendingCopies.erase(i);
        }
    }
    s.una = ack.cumulative;
    s.fack = std::max(s.fack, s.una);
    for (auto [left, right] : ack.sacks) {
        for (uint32_t i = left; i < right; ++i) {
            m_segments[i].sacked = true;
            if (m_segments[i].retransmitted) {
                m_segments[i].retransmitted = false;
                --s.retransmittedOutstanding; m_pendingCopies.erase(i);
            }
        }
        s.fack = std::max(s.fack, right);
    }

    s.nxt = std::max(s.nxt, s.una);
    if (s.recovery) {

        --s.sackPipe;
        if (r.newAck && s.una < s.recoverPoint) --s.sackPipe;
        if (r.newAck && s.una >= s.recoverPoint) {

            for (uint32_t i = s.una; i < s.highWater; ++i) m_segments[i] = {};
            m_pendingCopies.clear();
            s.retransmittedOutstanding = 0;
            s.fack = s.una;
            s.recovery = false; s.duplicateAcks = 0;
            r.exitedRecovery = true; Emit("recovery-exit");
        } else {
            ++s.duplicateAcks;

            uint32_t lost = 0;
            for (uint32_t i = m_recoveryBase; i < s.recoverPoint; ++i)
                if (m_segments[i].wasRetransmitted || (i >= s.una && !m_segments[i].sacked)) ++lost;
            const double budget = m_config.packetWindow ? std::floor(EffectiveWindow()) : EffectiveWindow();
            if (m_config.sackStarvationFallback && repairedHole &&
                s.sackPipe >= budget &&
                2 * lost > s.recoverPoint - m_recoveryBase)
                return Restart("sack-fallback");
        }
        Emit("ack", ack.cumulative); Pump(r); return r;
    }
    if (r.newAck) {
        s.duplicateAcks = 0; IncreaseWindow();
    } else if (s.una < s.highWater) {
        ++s.duplicateAcks;
        if (s.duplicateAcks == 3 && s.una >= m_renoHoldUntil) {
            s.sackPipe = int32_t(std::floor(s.cwnd)) - 3;
            s.cwnd = CutWindow(.5);
            s.ssthresh = std::max(2.0, s.cwnd); s.recovery = true;
            s.recoverPoint = s.nxt; m_recoveryBase = s.una;
            r.enteredRecovery = true;
            Emit("recovery-enter");

            Send(r, s.una, true);
        }
    }
    Emit("ack", ack.cumulative);
    if (s.duplicateAcks == 0) Pump(r);
    return r;
}
Result Controller::Timeout() { return Restart("timeout"); }
Result Controller::Restart(const std::string& event) {
    auto& s = m_state;
    Result r;
    if (s.una >= s.highWater) return r;
    s.ssthresh = std::max(2.0, CutWindow(.5));
    s.cwnd = 1;
    s.recovery = false;
    s.wintrim = 0;
    s.duplicateAcks = s.renoInflation = s.retransmittedOutstanding = 0;
    s.sackPipe = 0;
    m_pendingCopies.clear();
    m_restartCopies.clear();

    for (uint32_t i = s.una; i < s.highWater; ++i) m_segments[i] = {};

    s.nxt = s.fack = s.una;

    if (m_config.algorithm != Algorithm::Fack) m_renoHoldUntil = s.highWater + 1;
    Emit(event);
    Pump(r);
    return r;
}

Ack SackReceiver::Receive(uint32_t sequence) {
    if (m_received.size() <= sequence) m_received.resize(sequence + 1);
    if (m_received[sequence]) ++m_duplicates;
    m_received[sequence] = true;
    while (m_next < m_received.size() && m_received[m_next]) ++m_next;
    auto block = [&](uint32_t i) {
        uint32_t l = i, h = i + 1;
        while (l > m_next && m_received[l - 1]) --l;
        while (h < m_received.size() && m_received[h]) ++h;
        return std::make_pair(l, h);
    };
    std::vector<std::pair<uint32_t, uint32_t>> current;
    if (sequence > m_next) current.push_back(block(sequence));
    for (auto [l, h] : m_recent) {
        if (h <= m_next || l < m_next || !m_received[l]) continue;
        auto b = block(l);
        if (std::find(current.begin(), current.end(), b) == current.end()) current.push_back(b);
    }
    m_recent = current;
    if (current.size() > 3) current.resize(3);
    return {m_next, current};
}
}
