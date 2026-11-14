#include <cstdint>
#include <cstring>
#include <deque>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include "../model/fack-model.h"
#include <cmath>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
using namespace fack1996;
namespace {
uint32_t checks = 0, cases = 0;
void Require(bool condition, const std::string& message) {
    ++checks;
    if (!condition) throw std::runtime_error(message);
}
void Equal(double actual, double expected, const std::string& message) {
    Require(std::abs(actual - expected) < 1e-9, message);
}
template<class F> void Test(const std::string& name, F f) {
    f(); ++cases; std::cout << "PASS " << name << '\n';
}
Config Window(double cwnd = 8) { Config c; c.initialCwnd = cwnd; return c; }
}
namespace history_regression {

using namespace fack1996;
namespace {
uint64_t digest = 1469598103934665603ULL;
uint64_t observations = 0;
void Hash(uint64_t value) {
    for (int i = 0; i < 8; ++i) {
        digest ^= (value >> (i * 8)) & 255;
        digest *= 1099511628211ULL;
    }
}
void HashDouble(double value) {
    uint64_t bits;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    Hash(bits);
}
void Observe(const std::string& event, const State& s, int64_t sequence) {
    for (unsigned char ch : event) Hash(ch);
    Hash(sequence); Hash(s.una); Hash(s.nxt); Hash(s.fack); Hash(s.highWater);
    Hash(s.recoverPoint); Hash(s.duplicateAcks); Hash(s.renoInflation);
    Hash(s.retransmittedOutstanding); Hash(uint32_t(s.sackPipe));
    HashDouble(s.cwnd); HashDouble(s.ssthresh); HashDouble(s.wintrim);
    HashDouble(s.winmult); Hash(s.recovery); ++observations;
}
void Check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
void Queue(std::deque<Transmission>& flight, const Result& result) {
    Hash(result.newAck); Hash(result.enteredRecovery);
    Hash(result.exitedRecovery); Hash(result.forcedTimeout);
    Hash(result.transmissions.size());
    for (auto packet : result.transmissions) {
        Hash(packet.sequence); Hash(packet.retransmission); flight.push_back(packet);
    }
}

void LossyFlow(Algorithm algorithm, bool integerCut, bool packetWindow) {
    Config config; config.algorithm = algorithm;
    config.integerCut = integerCut; config.packetWindow = packetWindow;
    config.overdamping = config.rampdown = algorithm == Algorithm::Fack;
    Controller controller(config, Observe);
    SackReceiver receiver;
    std::deque<Transmission> flight;
    std::vector<bool> lostRepair;
    Queue(flight, controller.Start());
    uint32_t count = 0;
    for (; receiver.Next() < 50000 && count < 400000; ++count) {
        if (flight.empty()) {
            Queue(flight, controller.Timeout());
            continue;
        }
        auto packet = flight.front(); flight.pop_front();

        bool drop = !packet.retransmission && packet.sequence % 97 == 50;

        drop = drop || (!packet.retransmission && packet.sequence % 997 == 51);
        if (lostRepair.size() <= packet.sequence) lostRepair.resize(packet.sequence + 1);
        if (packet.retransmission && packet.sequence % 970 == 50 &&
            !lostRepair[packet.sequence]) {
            lostRepair[packet.sequence] = true;
            drop = true;
        }
        if (drop) { Hash(0xd0); Hash(packet.sequence); continue; }
        Ack ack = receiver.Receive(packet.sequence);
        if (algorithm == Algorithm::Reno) ack.sacks.clear();
        Result result = controller.Receive(ack);
        Queue(flight, result);
        if (result.forcedTimeout) Queue(flight, controller.Timeout());
        controller.CheckInvariants();
    }
    Check(receiver.Next() >= 50000, "FIFO loss flow did not progress");
    Hash(receiver.Next()); Hash(receiver.DuplicateSegments()); Hash(count);
}

void LongHistoryReset(Algorithm algorithm) {
    Config config; config.algorithm = algorithm;
    config.initialCwnd = 16; config.initialSsthresh = 16;
    config.receiverWindow = 32; config.packetWindow = true;
    Controller controller(config, Observe); controller.Start();
    while (controller.GetState().una < 100000)
        controller.Receive({controller.GetState().nxt, {}});
    uint32_t base = controller.GetState().una;
    uint32_t oldEnd = controller.GetState().nxt;
    Check(oldEnd >= base + 8, "long-history flight too small");
    if (algorithm == Algorithm::RenoSack) {
        controller.Receive({base, {{base + 1, base + 2}}});
        controller.Receive({base, {{base + 1, base + 3}}});
        controller.Receive({base, {{base + 1, base + 4}}});
        controller.Receive({base, {{base + 1, oldEnd}}});
        auto completed = controller.Receive({oldEnd, {}});
        Check(completed.exitedRecovery, "Sack1 long-history recovery did not exit");
    }
    auto restart = controller.Timeout();
    Check(restart.transmissions.size() == 1, "timeout restart sent wrong budget");
    Check(restart.transmissions.front().sequence == controller.GetState().una,
          "timeout did not restart at UNA");
    controller.Receive({controller.GetState().highWater, {}});
    controller.CheckInvariants();
}
}
int Run() {
    for (auto algorithm : {Algorithm::Reno, Algorithm::RenoSack, Algorithm::Fack}) {
        for (bool integer : {false, true})
            for (bool packets : {false, true}) LossyFlow(algorithm, integer, packets);
        LongHistoryReset(algorithm);
    }
    std::cout << "PASS 12 deterministic loss-flow configurations and three "
                 "long-history reset cases\n";
    std::cout << "observations=" << observations << " digest=" << std::hex << digest << '\n';
    return 0;
}
}

namespace timer_regression {
using fack1996::Ns1TimestampTimer;
int checks=0;
void Check(bool value) { ++checks; if (!value) throw std::runtime_error("ns1 timer test failed"); }
void Near(double a,double b) { Check(std::abs(a-b)<1e-12); }
template<class F> void Reject(F f) {
    bool rejected=false;try { f(); } catch (const std::exception&) { rejected=true; }
    Check(rejected);
}
int Run() {
    Ns1TimestampTimer t(.1);
    Check(t.rttTicks==0 && t.srttScaled==0 && t.varianceScaled==30);
    Near(t.Delay(1),3.0);

    double firstAckArm=t.Delay(1); t.Update(.087210666);
    Near(firstAckArm,3.0);
    Check(t.rttTicks==1 && t.srttScaled==8 && t.varianceScaled==2);
    Near(t.Delay(1),.3);
    t.Update(.087210666);
    Check(t.srttScaled==8 && t.varianceScaled==2);
    t.Update(.24);
    Check(t.rttTicks==2 && t.srttScaled==9 && t.varianceScaled==3);
    Near(t.Delay(1),.4);

    uint32_t backoff=1; double armed=t.Delay(backoff);t.Backoff(backoff);
    Near(armed,.4); Check(backoff==2); Near(t.Delay(backoff),.8);
    t.Backoff(backoff);t.Backoff(backoff);Check(backoff==8 && t.srttScaled==9);
    t.Backoff(backoff);Check(backoff==16 && t.srttScaled==0 && t.varianceScaled==3);
    t.Update(.3);Check(t.rttTicks==3 && t.srttScaled==24 && t.varianceScaled==6);
    Near(t.Delay(1),.9);
    t.srttScaled=1;t.varianceScaled=1;Near(t.Delay(1),.2);
    backoff=64;t.Backoff(backoff);Check(backoff==64);

    uint32_t probe=10;Check(!(10>probe));Check(11>probe);

    Ns1TimestampTimer r(.1);r.Update(.149);Check(r.rttTicks==1);r.Update(.151);Check(r.rttTicks==2);
    for (double bad : {0.0,-.1,1e-20,4.0,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()})
        Reject([=] { Ns1TimestampTimer invalid(bad); });
    for (double bad : {-1.0,1e100,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()})
        Reject([&] { r.Update(bad); });
    for (uint32_t bad : {0u,3u,128u}) {
        Reject([&] { r.Delay(bad); });
        Reject([&] { auto factor=bad;r.Backoff(factor); });
    }
    Ns1TimestampTimer coarse(2); Near(coarse.BaseRto(),2); Near(coarse.Delay(1),4);
    std::cout<<"ns1-timer: "<<checks<<" checks PASS\n";
    return 0;
}
}

int main() {
    Test("Slow-start increases once per ACK, not once per newly ACKed segment", [] {
        Controller c(Window(4)); c.Start(); c.Receive({2, {}});
        Equal(c.GetState().cwnd, 5, "one ACK acknowledging two packets must add one");
        Equal(c.GetState().fack, 2, "without SACK FACK follows cumulative ACK");
    });
    Test("one loss: no retransmission on duplicate ACK 1/2; third triggers", [] {
        Controller c(Window()); c.Start();
        auto one = c.Receive({0, {{1, 2}}});
        auto two = c.Receive({0, {{1, 3}}});
        Require(one.transmissions.empty() && two.transmissions.empty(), "premature retransmission");
        auto three = c.Receive({0, {{1, 4}}});
        Require(three.enteredRecovery, "third duplicate ACK did not trigger");
        Equal(c.GetState().cwnd, 4, "basic FACK must halve cwnd");
        Equal(c.GetState().recoverPoint, 8, "recover point must be original NXT");
        Require(three.transmissions.empty(), "awnd equals cwnd, sender must wait");
    });
    Test("SACK gap >3 can trigger before third duplicate ACK", [] {
        Controller c(Window()); c.Start(); auto r = c.Receive({0, {{4, 5}}});
        Require(r.enteredRecovery && c.GetState().duplicateAcks == 1, "FACK early trigger absent");
        Require(r.transmissions.size() == 1 && r.transmissions[0].sequence == 0,
                "must retransmit oldest hole");
        Equal(c.Outstanding(), 4, "eq (2) after first retransmission");
    });
    Test("retran_data is decremented once by SACK and once by cumulative ACK", [] {
        Controller c(Window()); c.Start(); c.Receive({0, {{4, 5}}});
        auto r = c.Receive({0, {{4, 6}}});
        Require(r.transmissions.at(0).sequence == 1, "wrong second hole");
        c.Receive({1, {{4, 6}}});
        Equal(c.GetState().retransmittedOutstanding, 2, "cumulative ACK accounting");
        c.Receive({1, {{2, 3}, {4, 6}}});
        Equal(c.GetState().retransmittedOutstanding, 2, "SACK accounting");
        c.Receive({1, {{2, 3}, {4, 6}}});
        Equal(c.GetState().retransmittedOutstanding, 2, "repeated SACK decremented twice");
    });
    Test("cwnd is frozen and partial ACK cannot end FACK recovery", [] {
        Controller c(Window()); c.Start(); c.Receive({0, {{1, 5}}});
        c.Receive({5, {}});
        Require(c.GetState().recovery, "partial ACK ended recovery");
        Equal(c.GetState().cwnd, 4, "cwnd grew inside recovery");
        auto r = c.Receive({8, {}});
        Require(r.exitedRecovery && !c.GetState().recovery, "full ACK must end recovery");
        Equal(c.GetState().cwnd, 4, "exit ACK incorrectly increased cwnd");
        c.Receive({9, {}});
        Equal(c.GetState().cwnd, 4.25, "Congestion Avoidance after recovery");
    });
    Test("Overdamping uses 1/4 after Slow-start, but 1/2 in Congestion Avoidance", [] {
        Config config = Window(16); config.overdamping = true;
        Controller slow(config); slow.Start(); slow.Receive({0, {{1, 5}}});
        Equal(slow.GetState().cwnd, 4, "Overdamping quarter reduction");
        config.initialSsthresh = 8;
        Controller avoidance(config); avoidance.Start(); avoidance.Receive({0, {{1, 5}}});
        Equal(avoidance.GetState().cwnd, 8, "Overdamping should not quarter CA window");
    });
    Test("Overdamping includes the paper's half-segment boundary before setting ssthresh", [] {
        for (double window : {8.5, 8.500001}) {
            Config config = Window(window); config.initialSsthresh = 8;
            config.overdamping = true;
            Controller c(config); c.Start(); c.Receive({0, {{1, 5}}});
            const double multiplier = window == 8.5 ? 0.25 : 0.5;
            Equal(c.GetState().winmult, multiplier, "paper section 4.4 boundary");
            Equal(c.GetState().cwnd, window * multiplier, "extra reduction before ordinary cut");
            Equal(c.GetState().ssthresh, window * multiplier,
                  "ssthresh must be set after both reductions, not before Overdamping");
        }
    });
    Test("Rampdown equations (3)/(4), repeated SACK has zero delta", [] {
        Config config = Window(16); config.rampdown = true; config.overdamping = true;
        Controller c(config); c.Start(); c.Receive({0, {{1, 5}}});
        Equal(c.GetState().winmult, 0.25, "wrong winmult");
        Equal(c.GetState().wintrim, (16 - 5) * 0.75, "equation (3)");
        c.Receive({0, {{1, 7}}});
        Equal(c.GetState().wintrim, (16 - 7) * 0.75, "equation (4)");
        double trim = c.GetState().wintrim;
        c.Receive({0, {{1, 7}}}); Equal(c.GetState().wintrim, trim, "repeated SACK reduced trim");
    });
    Test("Rampdown has no one-packet-per-ACK pacing rule", [] {
        Config config = Window(32); config.rampdown = true;
        Controller c(config); c.Start(); c.Receive({0, {{24, 25}}});
        auto r = c.Receive({0, {{24, 31}}});
        Require(r.transmissions.size() > 1, "unpublished pacing guard returned");
    });
    Test("lost retransmission requires FACK strictly beyond captured NXT", [] {
        Controller c(Window()); c.Start(); c.Receive({0, {{1, 5}}});
        auto equal = c.Receive({0, {{1, 8}}});
        Require(!equal.forcedTimeout, "equality must not force timeout");
        auto beyond = c.Receive({0, {{1, 9}}});
        Require(beyond.forcedTimeout, "lost retransmission was not detected");
        c.Timeout();
        Equal(c.GetState().cwnd, 1, "forced RTO must restart Slow-start");
        Equal(c.GetState().retransmittedOutstanding, 0, "timeout scoreboard not cleared");
    });
    Test("receiver window constrains new data; SACKed data is never retransmitted", [] {
        Config config = Window(16); config.receiverWindow = 8;
        Controller c(config); Require(c.Start().transmissions.size() == 8, "receiver window ignored");
        auto r = c.Receive({0, {{1, 8}}});
        Require(r.transmissions.size() == 1 && r.transmissions[0].sequence == 0,
                "must fill oldest hole only, no SACKed/new data beyond receiver window");
    });
    Test("timeout restart progresses; late pre-timeout SACK cannot underflow NXT-FACK", [] {
        Controller c(Window()); c.Start(); auto r = c.Timeout();
        Require(r.transmissions.size() == 1 && r.transmissions[0].sequence == 0, "RTO resend");
        c.Receive({0, {{4, 8}}});
        c.CheckInvariants();
        Require(c.GetState().nxt >= c.GetState().fack, "unsigned underflow after stale flight evidence");
    });
    Test("receiver merges blocks, orders newest first, caps SACK options at three", [] {
        SackReceiver r;
        for (uint32_t s : {2u, 4u, 6u, 8u}) r.Receive(s);
        Ack a = r.Receive(9);
        Require(a.sacks.size() == 3 && a.sacks[0] == std::make_pair(8u, 10u), "SACK ordering/cap");
        r.Receive(1); a = r.Receive(0);
        Require(a.cumulative == 3, "cumulative ACK failed to skip contiguous buffered data");
        r.Receive(2); Require(r.DuplicateSegments() == 1, "duplicate-data count");
    });
    Test("classic Reno exits on a partial ACK; FACK does not", [] {
        Config config = Window(); config.algorithm = Algorithm::Reno;
        Controller c(config); c.Start();
        c.Receive({0, {}}); c.Receive({0, {}}); auto fast = c.Receive({0, {}});
        Require(fast.enteredRecovery && fast.transmissions.at(0).sequence == 0, "Reno FR trigger");
        c.Receive({1, {}});
        Require(!c.GetState().recovery, "classic Reno must exit on first new ACK");
    });
    Test("invalid ACK/SACK of unsent data is rejected", [] {
        Controller c(Window()); c.Start();
        bool rejected = false; try { c.Receive({9, {}}); } catch (const std::invalid_argument&) { rejected = true; }
        Require(rejected, "invalid cumulative ACK accepted");
        rejected = false; try { c.Receive({0, {{1, 9}}}); } catch (const std::invalid_argument&) { rejected = true; }
        Require(rejected, "invalid SACK accepted");
    });
    Test("packet-window sensitivity rounds sending budget but preserves real cwnd", [] {
        Config config = Window(4.5); config.packetWindow = true;
        Controller whole(config); Require(whole.Start().transmissions.size() == 4, "ns-1 packet rounding");
        Equal(whole.GetState().cwnd, 4.5, "real cwnd should retain fraction");
        config.packetWindow = false;
        Controller strict(config); Require(strict.Start().transmissions.size() == 5, "paper while-loop budget");
    });
    Test("Reno+SACK uses an ACK-counted pipe, not FACK's forward edge", [] {
        Config config = Window(); config.algorithm = Algorithm::RenoSack;
        Controller c(config); c.Start();
        c.Receive({0, {{1, 2}}}); c.Receive({0, {{1, 3}}});
        auto r = c.Receive({0, {{1, 4}}});
        Require(r.enteredRecovery && r.transmissions.size() == 1, "SACK fast retransmit");
        Equal(c.GetState().sackPipe, 5, "initial pipe = old cwnd - three ACKs");
        c.Receive({0, {{1, 5}}});
        Equal(c.GetState().sackPipe, 4, "one returning ACK removes one pipe unit");
        c.Receive({5, {}});
        Require(c.GetState().recovery, "SACK partial ACK ended recovery");
        c.Receive({8, {}});
        Require(!c.GetState().recovery, "SACK full ACK did not end recovery");
    });
    Test("Sack1 exits at its recovery boundary even when later data remains SACKed", [] {
        for (bool pastBoundary : {false, true}) {
            Config config = Window(); config.algorithm = Algorithm::RenoSack;
            Controller c(config); c.Start();
            c.Receive({0, {{1, 2}}}); c.Receive({0, {{1, 3}}});
            c.Receive({0, {{1, 4}}}); c.Receive({0, {{1, 5}}});
            c.Receive({5, {}});
            if (pastBoundary) c.Receive({6, {}});
            const uint32_t cumulative = pastBoundary ? 9 : 8;
            const uint32_t right = pastBoundary ? 12 : 10;
            auto exit = c.Receive({cumulative, {{cumulative + 1, right}}});
            Require(exit.exitedRecovery && !c.GetState().recovery,
                    "later-data SACK incorrectly prolonged completed recovery");
            Equal(c.GetState().cwnd, 4, "recovery-exit ACK must not grow cwnd");
            Equal(c.GetState().fack, cumulative, "Sack1 must clear its completed scoreboard");
            Equal(c.GetState().retransmittedOutstanding, 0, "Sack1 exit retained old retransmission state");
            Require(!exit.transmissions.empty(), "normal ACK-clock did not resume after recovery");
            for (auto t : exit.transmissions)
                Require(!t.retransmission, "completed recovery retransmitted a later-data hole");
            c.CheckInvariants();
        }
    });
    Test("ns-1 timeout guard requires ACK strictly beyond the old flight", [] {
        for (auto algorithm : {Algorithm::Reno, Algorithm::RenoSack}) {
            Config config = Window(); config.algorithm = algorithm;
            Controller c(config); c.Start(); c.Timeout();
            c.Receive({8, {}});
            c.Receive({8, {}}); c.Receive({8, {}});
            auto boundary = c.Receive({8, {}});
            Require(!boundary.enteredRecovery && !c.GetState().recovery,
                    "ACK equal to old high-water must not pass ns-1's strict guard");
            for (auto t : boundary.transmissions)
                Require(!t.retransmission, "guarded duplicate ACK triggered Fast Retransmit");
            c.Receive({9, {}});
            c.Receive({9, {}}); c.Receive({9, {}});
            auto beyond = c.Receive({9, {}});
            Require(beyond.enteredRecovery && c.GetState().recovery,
                    "ACK beyond old high-water did not release timeout guard");
            Require(!beyond.transmissions.empty() && beyond.transmissions[0].retransmission &&
                    beyond.transmissions[0].sequence == 9,
                    "released guard did not retransmit oldest outstanding segment");
        }
    });
    Test("Reno+SACK >half-window ACK starvation enters Slow-start directly", [] {
        Config config = Window(16); config.algorithm = Algorithm::RenoSack;
        config.sackStarvationFallback = true;
        Controller c(config); c.Start();
        for (uint32_t right = 12; right <= 16; ++right) c.Receive({0, {{11, right}}});
        auto r = c.Receive({1, {{11, 16}}});
        Equal(c.GetState().cwnd, 1, "starvation fallback must enter Slow-start");
        Require(!c.GetState().recovery && r.transmissions.at(0).sequence == 1, "fallback restart");
        Equal(c.GetState().retransmittedOutstanding, 0, "fallback must discard scoreboard");
    });
    Test("bursty application offers data to a persistent TCP controller", [] {
        Config config; config.algorithm = Algorithm::Reno; config.saturated = false;
        Controller c(config); Require(c.Start().transmissions.empty(), "empty application sent data");
        auto r = c.OfferSegments(3);
        Require(r.transmissions.size() == 1, "first reverse burst must obey initial cwnd");
        c.Receive({1,{}}); c.Receive({3,{}});
        Require(c.GetState().nxt == 3, "source exceeded application data");
        Require(c.OfferSegments(2).transmissions.size() == 2, "next burst should reuse TCP window");
    });
    Test("old-flight SACK after RTO neither falsely loses nor resends timeout copy", [] {
        Controller c(Window(12)); c.Start(); c.Timeout();
        auto one=c.Receive({0,{{4,9}}});
        Require(!one.forcedTimeout,"old ACK falsely lost timeout copy");
        for (auto t:one.transmissions) Require(t.sequence!=0,"timeout copy resent before acknowledgement");
        Equal(c.GetState().recoverPoint,12,"recovery boundary must use true next-unsent");
        Require(c.GetState().cwnd<=1,"old-flight loss signal increased cwnd after RTO");
        auto two=c.Receive({0,{{4,10}}});
        Require(!two.forcedTimeout,"old FACK passed cursor, not high-water boundary");
        for (auto t:two.transmissions) Require(t.sequence!=0,"pending timeout copy transmitted twice");
        c.CheckInvariants();
    });
    Test("stock Reno+SACK waits for its timer when the ACK-counted pipe starves", [] {
        Config config = Window(16); config.algorithm = Algorithm::RenoSack;
        Require(!config.sackStarvationFallback, "unpublished fallback must be opt-in");
        Controller c(config); c.Start();
        for (uint32_t right = 12; right <= 16; ++right) c.Receive({0, {{11, right}}});
        auto blocked = c.Receive({1, {{11, 16}}});
        Require(c.GetState().recovery && blocked.transmissions.empty(), "stock Sack1 silently restarted");
        Equal(c.GetState().cwnd, 8, "ACK starvation alone must not reduce baseline cwnd");
        auto timeout = c.Timeout();
        Equal(c.GetState().cwnd, 1, "actual RTO must enter Slow-start");
        Require(timeout.transmissions.size() == 1 && timeout.transmissions[0].sequence == 1,
                "RTO restart did not resend oldest unacknowledged data");
    });
    Test("sending-budget rounding and cut rounding are shared independent policies", [] {
        for (auto algorithm : {Algorithm::Reno, Algorithm::RenoSack, Algorithm::Fack}) {
            for (bool packets : {false, true}) {
                for (bool integer : {false, true}) {
                    Config config = Window(9); config.algorithm = algorithm;
                    config.packetWindow = packets; config.integerCut = integer;
                    Controller c(config); c.Start();
                    if (algorithm == Algorithm::Reno) {
                        c.Receive({0,{}}); c.Receive({0,{}}); c.Receive({0,{}});
                    } else {
                        c.Receive({0,{{1,2}}}); c.Receive({0,{{1,3}}}); c.Receive({0,{{1,4}}});
                    }
                    Equal(c.GetState().cwnd, integer ? 4 : 4.5, "algorithm-specific cut convention");
                    Equal(c.GetState().ssthresh, integer ? 4 : 4.5, "algorithm-specific threshold convention");
                    c.Timeout();
                    Equal(c.GetState().ssthresh, integer ? 2 : 2.25, "algorithm-specific RTO cut convention");
                }
            }
        }
    });
    Test("all controllers obey the same fractional sending budget when selected", [] {
        for (auto algorithm : {Algorithm::Reno, Algorithm::RenoSack, Algorithm::Fack}) {
            Config config = Window(4.5); config.algorithm = algorithm;
            Controller real(config);
            Require(real.Start().transmissions.size() == 5, "real budget differs by algorithm");
            config.packetWindow = true;
            Controller whole(config);
            Require(whole.Start().transmissions.size() == 4, "packet budget differs by algorithm");
        }
    });
    Test("optional SACK fallback requires a truly blocked selected sending budget", [] {
        for (bool packets : {false, true}) {
            Config config = Window(17); config.algorithm = Algorithm::RenoSack;
            config.sackStarvationFallback = true; config.packetWindow = packets;
            Controller c(config); c.Start();
            for (uint32_t right = 11; right <= 17; ++right) c.Receive({0,{{10,right}}});
            c.Receive({1,{{10,17}}});
            if (packets) Require(!c.GetState().recovery && c.GetState().cwnd == 1,
                                 "whole-packet blocked pipe did not use opted-in fallback");
            else Require(c.GetState().recovery && c.GetState().cwnd == 8.5,
                         "fractional budget still has room; fallback was premature");
        }
    });
    Test("loss cuts respect flow-control ceiling and preserve minimum ssthresh", [] {
        for (auto algorithm : {Algorithm::Reno, Algorithm::RenoSack, Algorithm::Fack}) {
            Config config = Window(16); config.algorithm = algorithm; config.receiverWindow = 8;
            Controller c(config); Require(c.Start().transmissions.size() == 8, "flow-control ceiling");
            if (algorithm == Algorithm::Reno) {
                c.Receive({0,{}}); c.Receive({0,{}}); c.Receive({0,{}});
            } else {
                c.Receive({0,{{1,2}}}); c.Receive({0,{{1,3}}}); c.Receive({0,{{1,4}}});
            }
            Equal(c.GetState().cwnd, 4, "loss halved an unavailable receiver-limited window");
            c.Timeout();
            Require(c.GetState().ssthresh >= 2, "minimum Slow-start threshold");
        }
        Controller fack(Window()); fack.Start(); fack.Timeout(); fack.Receive({0,{{4,8}}});
        Equal(fack.GetState().cwnd, 1, "old loss signal must not inflate cwnd");
        Equal(fack.GetState().ssthresh, 2, "minimum threshold after small FACK cut");
    });
    Test("nonfinite initial windows are rejected before simulation", [] {
        for (double value : {std::numeric_limits<double>::quiet_NaN(), std::numeric_limits<double>::infinity()}) {
            Config config = Window(value);
            bool rejected = false;
            try { Controller c(config); } catch (const std::invalid_argument&) { rejected = true; }
            Require(rejected, "nonfinite cwnd accepted");
            config = Window(); config.initialSsthresh = value; rejected = false;
            try { Controller c(config); } catch (const std::invalid_argument&) { rejected = true; }
            Require(rejected, "nonfinite ssthresh accepted");
        }
    });
    std::cout << "PASS " << cases << " cases, " << checks << " checks\n";
    history_regression::Run();
    std::cout << std::dec;
    timer_regression::Run();
}
