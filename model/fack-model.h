/*
The Reno and Reno+SACK controllers and optional timer are adapted from
LBNL ns-1.1 (tcp.cc, tcp-reno.cc, tcp-sack1.cc; tcp-fack.cc is also a reference):
https://ftp.gwdg.de/pub/linux/tux/net/ftp.ee.lbl.gov/ns/ns-1.1.tar.gz

This software includes adaptations of software developed by the University
of California, Lawrence Berkeley Laboratory, Berkeley, CA.

Copyright (c) 1990 Regents of the University of California.
All rights reserved.

Redistribution and use in source and binary forms are permitted
provided that the above copyright notice and this paragraph are
duplicated in all such forms and that any documentation,
advertising materials, and other materials related to such
distribution and use acknowledge that the software was developed
by the University of California, Lawrence Berkeley Laboratory,
Berkeley, CA. The name of the University may not be used to
endorse or promote products derived from this software without
specific prior written permission.
THIS SOFTWARE IS PROVIDED ``AS IS'' AND WITHOUT ANY EXPRESS OR
IMPLIED WARRANTIES, INCLUDING, WITHOUT LIMITATION, THE IMPLIED
WARRANTIES OF MERCHANTIBILITY AND FITNESS FOR A PARTICULAR PURPOSE.
*/
#ifndef FACK_1996_MODEL_H
#define FACK_1996_MODEL_H

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <cstdint>
#include <functional>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace fack1996 {
enum class Algorithm { Reno, RenoSack, Fack };
struct Config {
    Algorithm algorithm{Algorithm::Fack};
    bool overdamping{false}, rampdown{false};
    bool packetWindow{false};
    bool integerCut{false};
    bool sackStarvationFallback{false};
    double initialCwnd{1}, initialSsthresh{1000};
    uint32_t receiverWindow{100};
    uint32_t totalSegments{0};
    bool saturated{true};
};
struct Ack {
    uint32_t cumulative{0};
    std::vector<std::pair<uint32_t, uint32_t>> sacks;
};
struct State {
    uint32_t una{0}, nxt{0}, fack{0}, highWater{0};
    uint32_t duplicateAcks{0}, recoverPoint{0}, renoInflation{0};
    uint32_t retransmittedOutstanding{0};
    int32_t sackPipe{0};
    double cwnd{1}, ssthresh{1000}, wintrim{0}, winmult{0.5};
    bool recovery{false};
};
struct Transmission { uint32_t sequence; bool retransmission; };
struct Result {
    bool newAck{false}, enteredRecovery{false}, exitedRecovery{false};
    bool forcedTimeout{false};
    std::vector<Transmission> transmissions;
};
using Observer = std::function<void(const std::string&, const State&, int64_t)>;

class Controller {
  public:
    explicit Controller(Config config, Observer observer = {});
    Result Start();
    Result OfferSegments(uint32_t additional);
    Result Receive(const Ack& ack);
    Result Timeout();
    const State& GetState() const { return m_state; }
    double Outstanding() const;
    double EffectiveWindow() const;

    void CheckInvariants() const;

  private:
    struct Segment { bool sacked{false}, retransmitted{false}, wasRetransmitted{false}; uint32_t nxtAtRetransmit{0}; };
    Config m_config;
    State m_state;
    std::vector<Segment> m_segments;
    std::set<uint32_t> m_pendingCopies;
    std::set<uint32_t> m_restartCopies;
    Observer m_observer;

    uint32_t m_renoHoldUntil{0};
    uint32_t m_applicationEnd{0}, m_recoveryBase{0};
    void Emit(const std::string& event, int64_t sequence = -1) const;
    void Grow(uint32_t size);
    void Send(Result& result, uint32_t sequence, bool selectiveRetransmission);
    void Pump(Result& result);
    void IncreaseWindow();
    double CutWindow(double multiplier) const;
    Result ReceiveFack(const Ack& ack);
    Result ReceiveReno(const Ack& ack);
    Result ReceiveSack(const Ack& ack);
    Result Restart(const std::string& event);
};

class SackReceiver {
  public:
    Ack Receive(uint32_t sequence);
    uint32_t Next() const { return m_next; }
    uint32_t DuplicateSegments() const { return m_duplicates; }
  private:
    uint32_t m_next{0}, m_duplicates{0};
    std::vector<bool> m_received;
    std::vector<std::pair<uint32_t, uint32_t>> m_recent;
};
}

namespace fack1996 {
struct Ns1TimestampTimer {
    double tick;
    int rttTicks=0, srttScaled=0, varianceScaled;
    static int Checked(int64_t value) {
        if (value<0 || value>std::numeric_limits<int>::max())
            throw std::overflow_error("ns1 fixed-point integer overflow");
        return int(value);
    }
    static void CheckBackoff(uint32_t factor) {
        if (!factor || factor>64 || (factor&(factor-1)))
            throw std::invalid_argument("invalid ns1 backoff factor");
    }
    explicit Ns1TimestampTimer(double t) : tick(t), varianceScaled(0) {

        if (!(t>0) || !std::isfinite(t)) throw std::invalid_argument("invalid ns1 tick");
        double initial=3.0/t;
        if (!std::isfinite(initial) || initial<1 || initial>std::numeric_limits<int>::max())
            throw std::invalid_argument("unsupported ns1 tick for 3-second integer initialization");
        varianceScaled=int(initial);
    }
    void Update(double sample) {
        if (!(sample>=0) || !std::isfinite(sample)) throw std::invalid_argument("invalid ns1 sample");
        double ticks=sample/tick+0.5;
        if (!std::isfinite(ticks) || ticks>std::numeric_limits<int>::max()/8)
            throw std::overflow_error("ns1 RTT sample exceeds supported fixed-point range");
        rttTicks=std::max(1,int(ticks));
        if (srttScaled!=0) {
            int delta=rttTicks-(srttScaled>>3);
            srttScaled=std::max(1,Checked(int64_t(srttScaled)+delta));
            delta=std::abs(delta)-(varianceScaled>>2);
            varianceScaled=std::max(1,Checked(int64_t(varianceScaled)+delta));
        } else {
            srttScaled=rttTicks<<3;
            varianceScaled=rttTicks<<1;
        }
    }
    double BaseRto() const { return (int64_t(srttScaled>>3)+varianceScaled)*tick; }
    double Delay(uint32_t backoff) const {
        CheckBackoff(backoff);
        double value=BaseRto()*backoff;

        if (!std::isfinite(value) || value>2000 || value< -2000)
            throw std::overflow_error("ns1 RTO overflow guard");
        return std::max(2*tick,value);
    }
    void Backoff(uint32_t& factor) {
        CheckBackoff(factor);
        if (factor<64) factor<<=1;
        if (factor>8) srttScaled=0;
    }
    double Srtt() const { return (srttScaled>>3)*tick; }
    double Rttvar() const { return varianceScaled*tick/4; }
};
}

#endif
