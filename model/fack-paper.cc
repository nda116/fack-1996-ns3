#include "fack-model.h"
#include "queue-probe.h"
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <memory>
#include <set>
#include <sstream>
using namespace ns3;
using namespace fack1996;
namespace {
constexpr uint16_t BULK_PROTOCOL=0x0800, TINY_PROTOCOL=0x86dd;
constexpr uint32_t DATA_BYTES=1000, ACK_BYTES=40;
struct WireHeader : Header {
    bool ack{false}, reverse{false}, retransmission{false};
    uint8_t count{0};
    uint32_t sequence{0}, blocks[3][2]{};
    uint64_t timestamp{0};
    static TypeId GetTypeId() {
        static TypeId tid=TypeId("ns3::FackPaperHeader").SetParent<Header>().AddConstructor<WireHeader>();
        return tid;
    }
    TypeId GetInstanceTypeId() const override { return GetTypeId(); }
    uint32_t GetSerializedSize() const override { return 38; }
    void Serialize(Buffer::Iterator i) const override {
        i.WriteU8(uint8_t(ack) | (uint8_t(reverse)<<1) | (uint8_t(retransmission)<<2));
        i.WriteHtonU32(sequence); i.WriteU8(count);
        for (const auto& b:blocks) { i.WriteHtonU32(b[0]); i.WriteHtonU32(b[1]); }
        i.WriteHtonU64(timestamp);
    }
    uint32_t Deserialize(Buffer::Iterator i) override {
        uint8_t flags=i.ReadU8(); ack=flags&1; reverse=flags&2; retransmission=flags&4;
        sequence=i.ReadNtohU32(); count=i.ReadU8();
        for (auto& b:blocks) { b[0]=i.ReadNtohU32(); b[1]=i.ReadNtohU32(); }
        timestamp=i.ReadNtohU64();
        NS_ABORT_MSG_IF(count>3,"corrupted SACK count"); return GetSerializedSize();
    }
    void Print(std::ostream& os) const override { os<<(ack?"ACK ":"DATA ")<<sequence; }
};
fack1996::Config config;
std::string algorithm="fack", output="results/manual", lossList,retransmissionLossList,ackLossList;
uint32_t queueLimit=17, extraWaitingSlots=0, tinyBurstBytes=0, seed=1;
double stopTime=5, warmup=0, tinyRate=2;
double initialRto=3, minRto=1, maxRto=60, timerTick=.1;
bool detail=true;

bool sackOptionBytes=true;
bool ns1TimestampTimer=false;
bool queueProbe=false;
uint32_t queueProbePackets=30;
std::set<uint32_t> injectedLosses;
std::set<uint32_t> injectedRetransmissionLosses,injectedAckLosses;
uint32_t injectedCount=0,injectedRetransmissionCount=0,injectedAckCount=0;
double Now() { return Simulator::Now().GetSeconds(); }
void AccountDelivery(bool reverse,uint32_t delivered,bool unique);
std::string Name(Algorithm a) { return a==Algorithm::Reno?"reno":a==Algorithm::RenoSack?"reno-sack":"fack"; }
void Open(std::ofstream& f,const std::string& directory,const std::string& name,const std::string& header) {
    f.open(directory+"/"+name); NS_ABORT_MSG_IF(!f,"cannot open output "<<name);
    f<<std::fixed<<std::setprecision(9)<<header<<'\n';
}
struct Flow {
    fack1996::Config cfg;
    bool reverse;
    std::string directory;
    uint16_t protocol;
    Ptr<NetDevice> senderDevice,receiverDevice;
    Controller controller;
    SackReceiver receiver;
    std::ofstream events,network,deliveries,acks,received,timers,timerAudit;
    EventId timer;
    double srtt=0,rttvar=0,rto=initialRto,firstAck=-1,probeSent=0;
    bool haveRtt=false,rttProbe=false;
    uint32_t rttProbeSequence=0,rttBlockedUntil=0,timeouts=0,forcedTimeouts=0,retransmissions=0;
    uint32_t drops=0,ackDrops=0,recoveries=0,fallbacks=0,observations=0;
    uint64_t deliveredInWindow=0,uniqueReceivedInWindow=0;
    uint32_t backoff=1;
    Ns1TimestampTimer ns1Timer{ns1TimestampTimer?timerTick:.1};
    uint64_t ns1Samples=0,ns1DuplicateSamples=0,ns1PartialAcks=0,ns1Backoffs=0;
    bool ns1RttActive=false;
    int64_t ns1RttSequence=-1,ns1MaxSequence=-1;
    void SyncNs1Timer() {
        srtt=ns1Timer.Srtt(); rttvar=ns1Timer.Rttvar(); rto=ns1Timer.BaseRto();
    }
    void Ns1Audit(const char* event,const char* reason,int64_t sequence=-1,
                  double echo=-1,double sample=-1,double delay=0,int sacks=-1) {
        if (!ns1TimestampTimer || !detail) return;
        const auto& s=controller.GetState();
        timerAudit<<Now()<<','<<event<<','<<reason<<','<<sequence<<','<<echo<<','<<sample
            <<','<<ns1Timer.rttTicks<<','<<ns1Timer.srttScaled<<','<<ns1Timer.varianceScaled
            <<','<<backoff<<','<<ns1RttActive<<','<<ns1RttSequence<<','<<ns1MaxSequence
            <<','<<s.una<<','<<s.recovery<<','<<s.recoverPoint<<','<<delay<<','<<sacks<<'\n';
    }
    void Ns1Backoff(const char* reason) {
        ns1Timer.Backoff(backoff); SyncNs1Timer(); ns1RttActive=false; ++ns1Backoffs;
        Ns1Audit("backoff",reason);
    }
    void Ns1Ack(const WireHeader& h,bool partialSack) {

        if (partialSack) {

            backoff=1; ++ns1PartialAcks;
            Ns1Audit("partial-ack","sack-partial",h.sequence,double(h.timestamp)*1e-9,-1,0,h.count);
            ArmTimer("sack-partial"); return;
        }
        ArmTimer("ack");
        if (ns1RttActive && int64_t(h.sequence)>ns1RttSequence) {
            ns1RttActive=false; backoff=1;
        }
        const double sample=Now()-double(h.timestamp)*1e-9;
        ns1Timer.Update(sample);
        SyncNs1Timer(); ++ns1Samples;
        if (h.sequence==controller.GetState().una) ++ns1DuplicateSamples;
        Ns1Audit("sample","ack",h.sequence,double(h.timestamp)*1e-9,sample,0,h.count);
    }
    Flow(fack1996::Config c,bool rev,std::string dir)
        : cfg(c),reverse(rev),directory(std::move(dir)),protocol(rev?TINY_PROTOCOL:BULK_PROTOCOL),
          controller(c,[this](const std::string& e,const State& s,int64_t seq){Observe(e,s,seq);}) {
        std::filesystem::create_directories(directory);
        if (!detail) return;
        Open(events,directory,"events.csv","time_s,event,segment,una,nxt,fack,high_water,retran_data,awnd,cwnd,ssthresh,wintrim,winmult,effective_window,dupacks,recovery,recover_point,sack_pipe,reno_inflation");
        Open(timers,directory,"timers.csv","time_s,event,srtt_s,rttvar_s,rto_s,backoff,delay_s");
        if (ns1TimestampTimer)
            Open(timerAudit,directory,"timer-profile.csv","time_s,event,reason,sequence,echo_s,sample_s,rtt_ticks,srtt_scaled,variance_scaled,backoff,active,probe_sequence,max_sequence,una,recovery,recover_point,delay_s,sack_count");
        Open(network,directory,"network.csv","time_s,event,segment,waiting_packets");
        Open(deliveries,directory,"delivery.csv","time_s,cumulative_segments");
        Open(acks,directory,"acks.csv","time_s,una,left1,right1,left2,right2,left3,right3");
        Open(received,directory,"receiver.csv","time_s,segment,cumulative_segments,duplicate");
        deliveries<<"0,0\n";
    }
    void Observe(const std::string& event,const State& s,int64_t sequence) {
        ++observations;
        if (event=="recovery-enter" || event=="reno-fast-retransmit") ++recoveries;
        if (event=="sack-fallback") ++fallbacks;
        if (!detail) return;
        events<<Now()<<','<<event<<','<<sequence<<','<<s.una<<','<<s.nxt<<','<<s.fack<<','<<s.highWater
            <<','<<s.retransmittedOutstanding<<','<<controller.Outstanding()<<','<<s.cwnd<<','<<s.ssthresh
            <<','<<s.wintrim<<','<<s.winmult<<','<<controller.EffectiveWindow()<<','<<s.duplicateAcks
            <<','<<s.recovery<<','<<s.recoverPoint<<','<<s.sackPipe<<','<<s.renoInflation<<'\n';
    }
    void ArmTimer(const char* reason="send") {
        Simulator::Cancel(timer);

        double base=std::min(maxRto,std::max(rto,minRto)*backoff);
        double delay=std::min(maxRto,std::ceil(base/timerTick-1e-9)*timerTick);
        if (ns1TimestampTimer) { SyncNs1Timer(); delay=ns1Timer.Delay(backoff); }
        if (detail) timers<<Now()<<",arm,"<<srtt<<','<<rttvar<<','<<rto<<','<<backoff<<','<<delay<<'\n';
        timer=Simulator::Schedule(Seconds(delay),&Flow::Timeout,this,false);
        Ns1Audit("arm",reason,-1,-1,-1,delay);
    }
    void Transmit(const Result& result) {
        for (const auto& t:result.transmissions) {
            if (ns1TimestampTimer) {
                if (t.retransmission) ++retransmissions;
                if (int64_t(t.sequence)>ns1MaxSequence) {
                    ns1MaxSequence=t.sequence;
                    if (!ns1RttActive) {
                        ns1RttActive=true;
                        ns1RttSequence=std::max(ns1RttSequence,int64_t(t.sequence));
                    }
                }
            } else if (t.retransmission) {
                ++retransmissions; rttProbe=false;

                rttBlockedUntil=controller.GetState().highWater;
            } else if (!rttProbe && controller.GetState().una>=rttBlockedUntil) {
                rttProbe=true; rttProbeSequence=t.sequence; probeSent=Now();
            }
            Ns1Audit("tx",t.retransmission?"retransmission":"new",t.sequence);
            WireHeader h; h.sequence=t.sequence; h.timestamp=Simulator::Now().GetNanoSeconds();
            h.reverse=reverse; h.retransmission=t.retransmission;
            auto p=Create<Packet>(DATA_BYTES-2-h.GetSerializedSize()); p->AddHeader(h);
            NS_ABORT_MSG_IF(!senderDevice->Send(p,Address(),protocol),"source access queue full");
            if (!timer.IsPending()) ArmTimer();
        }
    }
    void Start() { Transmit(controller.Start()); }
    void Offer(uint32_t segments) { Transmit(controller.OfferSegments(segments)); }
    void Timeout(bool forced) {
        if (controller.GetState().una>=controller.GetState().highWater) return;
        Ns1Audit(forced?"forced":"expiry",forced?"lost-repair":"rto");
        ++timeouts; if (forced) ++forcedTimeouts;
        if (detail) timers<<Now()<<','<<(forced?"forced-timeout":"timeout")<<','<<srtt<<','<<rttvar<<','<<rto<<','<<backoff<<",0\n";

        if (ns1TimestampTimer) {

            ArmTimer(forced?"forced-restart":"rto");
            if (!forced) Ns1Backoff("rto");
            else { ns1RttActive=false; Ns1Audit("restart","lost-repair"); }
        } else {
            if (!forced) backoff=std::min(64u,2*backoff);
            rttProbe=false; ArmTimer();
        }
        Transmit(controller.Timeout());
    }
    void ReceiveAck(Ptr<NetDevice>,Ptr<const Packet> packet,uint16_t,const Address&,const Address&,NetDevice::PacketType) {
        auto p=packet->Copy(); WireHeader h; p->RemoveHeader(h);
        if (!h.ack) return;
        NS_ABORT_MSG_IF(h.reverse!=reverse,"wrong connection ACK");
        if (firstAck<0) firstAck=Now();
        Ack a{h.sequence,{}};
        if (detail) acks<<Now()<<','<<h.sequence;
        for (uint32_t i=0;i<3;++i) {
            if (detail) acks<<','<<(i<h.count?int64_t(h.blocks[i][0]):-1)<<','<<(i<h.count?int64_t(h.blocks[i][1]):-1);
            if (i<h.count) a.sacks.emplace_back(h.blocks[i][0],h.blocks[i][1]);
        }
        if (detail) acks<<'\n';
        const auto before=controller.GetState();
        const bool advanced=h.sequence>before.una;
        Ns1Audit("ack","receive",h.sequence,double(h.timestamp)*1e-9,-1,0,h.count);
        if (ns1TimestampTimer) {

            if (h.sequence>=before.una && (advanced || cfg.algorithm==Algorithm::Fack)) {
                bool partialSack=cfg.algorithm==Algorithm::RenoSack && before.recovery
                    && advanced && h.sequence<before.recoverPoint;
                Ns1Ack(h,partialSack);
            }
        } else if (advanced) {
            if (rttProbe && h.sequence>rttProbeSequence) {
                double sample=Now()-probeSent;
                if (!haveRtt) { srtt=sample; rttvar=sample/2; haveRtt=true; }
                else { double error=sample-srtt; srtt+=error/8; rttvar+=(std::abs(error)-rttvar)/4; }
                rto=srtt+std::max(timerTick,4*rttvar); rttProbe=false;
            }
            backoff=1;
            ArmTimer();
        }
        Result r=controller.Receive(a);
        if (r.forcedTimeout) { Timeout(true); return; }
        if (r.enteredRecovery) {
            ArmTimer("fast-retransmit");
            if (ns1TimestampTimer) Ns1Backoff("fast-retransmit");
        }
        Transmit(r);
        if (controller.GetState().una>=controller.GetState().highWater) {
            Simulator::Cancel(timer); Ns1Audit("cancel","fully-acked");
        }
    }
    void ReceiveData(Ptr<NetDevice>,Ptr<const Packet> packet,uint16_t,const Address&,const Address&,NetDevice::PacketType) {
        auto p=packet->Copy(); WireHeader h; p->RemoveHeader(h);
        NS_ABORT_MSG_IF(h.ack || h.reverse!=reverse,"wrong connection DATA");
        uint32_t old=receiver.Next(),before=receiver.DuplicateSegments();
        Ack a=receiver.Receive(h.sequence);
        bool unique=receiver.DuplicateSegments()==before;
        AccountDelivery(reverse,a.cumulative-old,unique);
        if (Now()>=warmup && Now()<stopTime) {
            deliveredInWindow+=a.cumulative-old;
            if (unique) ++uniqueReceivedInWindow;
        }
        if (detail) {
            received<<Now()<<','<<h.sequence<<','<<a.cumulative<<','<<(receiver.DuplicateSegments()!=before)<<'\n';
            if (a.cumulative!=old) deliveries<<Now()<<','<<a.cumulative<<'\n';
        }
        WireHeader reply; reply.ack=true; reply.reverse=reverse; reply.sequence=a.cumulative; reply.timestamp=h.timestamp;
        reply.count=cfg.algorithm==Algorithm::Reno?0:a.sacks.size();
        for (uint32_t i=0;i<reply.count;++i) { reply.blocks[i][0]=a.sacks[i].first; reply.blocks[i][1]=a.sacks[i].second; }
        const uint32_t bytes=ACK_BYTES+(sackOptionBytes?8*reply.count:0);
        auto ack=Create<Packet>(bytes-2-reply.GetSerializedSize()); ack->AddHeader(reply);
        NS_ABORT_MSG_IF(!receiverDevice->Send(ack,Address(),protocol),"ACK access queue full");
    }
    void Summary(double forwardUtil,double reverseUtil) {
        controller.CheckInvariants();
        if (detail) { deliveries<<stopTime<<','<<receiver.Next()<<'\n'; deliveries.flush(); events.flush(); network.flush(); acks.flush(); received.flush(); timers.flush(); timerAudit.flush(); }
        std::ofstream s(directory+"/summary.json");
        s<<std::setprecision(12)<<"{\n\"algorithm\":\""<<Name(cfg.algorithm)<<"\",\n"
            <<"\"overdamping\":"<<cfg.overdamping<<",\n\"rampdown\":"<<cfg.rampdown<<",\n\"packet_window\":"<<cfg.packetWindow
            <<",\n\"integer_cut\":"<<cfg.integerCut
            <<",\n\"sack_option_bytes\":"<<sackOptionBytes
            <<",\n\"ns1_timestamp_timer\":"<<ns1TimestampTimer
            <<",\n\"ns1_timer_samples\":"<<ns1Samples
            <<",\n\"ns1_duplicate_ack_samples\":"<<ns1DuplicateSamples
            <<",\n\"ns1_partial_sack_acks\":"<<ns1PartialAcks
            <<",\n\"ns1_timer_backoffs\":"<<ns1Backoffs
            <<",\n\"timer_profile_trace\":"<<(ns1TimestampTimer&&detail)
            <<",\n\"timer_idle_policy\":\"cancel-fully-acked\""
            <<",\n\"timer_policy\":\""<<(ns1TimestampTimer?"ns1-timestamp-fixedpoint-partial":"shared-karn-jacobson")<<"\""
            <<",\n\"initial_rto_s\":"<<(ns1TimestampTimer?std::max(2*timerTick,int(3.0/timerTick)*timerTick):initialRto)
            <<",\n\"min_rto_s\":"<<(ns1TimestampTimer?2*timerTick:minRto)
            <<",\n\"max_rto_s\":"<<(ns1TimestampTimer?2000:maxRto)<<",\n\"timer_tick_s\":"<<timerTick
            <<",\n\"forced_restart_backoff\":\"preserve\""
            <<",\n\"receiver_window\":"<<cfg.receiverWindow
            <<",\n\"sack_starvation_fallback\":"<<cfg.sackStarvationFallback
            <<",\n\"queue\":"<<queueLimit<<",\n\"waiting_capacity\":"<<queueLimit-1+extraWaitingSlots
            <<",\n\"stop_s\":"<<stopTime<<",\n\"warmup_s\":"<<warmup<<",\n\"initial_cwnd\":"<<cfg.initialCwnd
            <<",\n\"initial_ssthresh\":"<<cfg.initialSsthresh<<",\n\"first_ack_s\":"<<firstAck
            <<",\n\"timeouts\":"<<timeouts<<",\n\"forced_timeouts\":"<<forcedTimeouts<<",\n\"retransmissions\":"<<retransmissions
            <<",\n\"rto_expiries\":"<<timeouts-forcedTimeouts
            <<",\n\"duplicate_segments\":"<<receiver.DuplicateSegments()<<",\n\"delivered_segments\":"<<receiver.Next()
            <<",\n\"drops\":"<<drops<<",\n\"ack_drops\":"<<ackDrops<<",\n\"injected_drops\":"<<(reverse?0:injectedCount)
            <<",\n\"injected_retransmission_drops\":"<<(reverse?0:injectedRetransmissionCount)
            <<",\n\"injected_ack_drops\":"<<(reverse?0:injectedAckCount)
            <<",\n\"model_segment_bytes\":"<<DATA_BYTES
            <<",\n\"delivered_in_window\":"<<deliveredInWindow
            <<",\n\"unique_received_in_window\":"<<uniqueReceivedInWindow
            <<",\n\"goodput_mbps\":"<<deliveredInWindow*DATA_BYTES*8.0/(stopTime-warmup)/1e6
            <<",\n\"recovery_count\":"<<recoveries<<",\n\"sack_fallbacks\":"<<fallbacks<<",\n\"core_invariant_checks\":"<<observations
            <<",\n\"tiny_burst_bytes\":"<<tinyBurstBytes<<",\n\"tiny_rate\":"<<tinyRate<<",\n\"seed\":"<<seed
            <<",\n\"detail\":"<<detail<<",\n\"reverse_connection\":"<<reverse
            <<",\n\"forward_utilization\":"<<forwardUtil<<",\n\"reverse_utilization\":"<<reverseUtil<<"\n}\n";
        std::cout<<(reverse?"tiny ":"bulk ")<<Name(cfg.algorithm)<<" Q="<<queueLimit<<" delivered="<<receiver.Next()
            <<" duplicates="<<receiver.DuplicateSegments()<<" timeouts="<<timeouts<<" fallbacks="<<fallbacks<<'\n';
    }
};
Flow *bulk=nullptr,*tiny=nullptr;
Ptr<Queue<Packet>> forwardQueue,reverseQueue;
std::ofstream linkLog,burstLog;
struct Bin {
    double forward=0,reverse=0,bulk=0,tiny=0;
    uint64_t delivered=0,received=0,tinyDelivered=0,tinyReceived=0;
};
std::vector<Bin> bins;
double forwardBusy=0,reverseDataBusy=0;
void AccountDelivery(bool reverse,uint32_t delivered,bool unique) {
    auto& bin=bins.at(size_t(Now()));
    (reverse?bin.tinyDelivered:bin.delivered)+=delivered;
    if (unique) ++(reverse?bin.tinyReceived:bin.received);
}
void Account(uint32_t direction,const WireHeader& h,uint32_t bytes) {
    double start=Now(),end=std::min(stopTime,start+DataRate("1.5Mbps").CalculateBytesTxTime(bytes).GetSeconds());
    double measured=std::max(0.0,end-std::max(start,warmup));
    if (!direction) forwardBusy+=measured;
    if (direction && h.reverse && !h.ack) reverseDataBusy+=measured;
    while (start<end-1e-12) {
        size_t i=size_t(start); double part=std::min(end,double(i+1))-start;
        if (!direction) { bins.at(i).forward+=part; if (!h.reverse&&!h.ack) bins.at(i).bulk+=part; }
        else { bins.at(i).reverse+=part; if (h.reverse&&!h.ack) bins.at(i).tiny+=part; }
        start+=part;
    }
}
void QueueEvent(uint32_t direction,std::string event,Ptr<const Packet> packet) {
    auto p=packet->Copy(); PppHeader ppp; p->RemoveHeader(ppp); WireHeader h; p->PeekHeader(h);
    auto queue=direction?reverseQueue:forwardQueue;
    Flow* f=h.reverse?tiny:bulk;
    if (!h.ack && detail && ((h.reverse && direction)||(!h.reverse && !direction)))
        f->network<<Now()<<','<<event<<','<<h.sequence<<','<<queue->GetNPackets()<<'\n';
    if (event=="drop") { if (h.ack) ++f->ackDrops; else ++f->drops; }
    if (event=="dequeue") Account(direction,h,packet->GetSize());
    if (detail) {
        linkLog<<Now()<<','<<direction<<','<<event<<','<<h.reverse<<','<<h.ack<<','<<h.sequence<<','<<packet->GetSize()<<','<<queue->GetNPackets();
        if (sackOptionBytes) linkLog<<','<<(h.ack?int(h.count):0);
        linkLog<<'\n';
    }
}
struct Forwarder {
    Ptr<NetDevice> out; bool inject=false,injectAck=false;
    void Receive(Ptr<NetDevice>,Ptr<const Packet> p,uint16_t protocol,const Address&,const Address&,NetDevice::PacketType) {
        if (inject) {
            WireHeader h; p->PeekHeader(h);

            if (!h.ack && !h.reverse && !h.retransmission && injectedLosses.erase(h.sequence)) {
                ++injectedCount; ++bulk->drops;
                if (detail) bulk->network<<Now()<<",injected-drop,"<<h.sequence<<','<<forwardQueue->GetNPackets()<<'\n';
                return;
            }
            if (!h.ack && !h.reverse && h.retransmission && injectedRetransmissionLosses.erase(h.sequence)) {
                ++injectedRetransmissionCount; ++bulk->drops;
                if (detail) bulk->network<<Now()<<",injected-retransmission-drop,"<<h.sequence<<','<<forwardQueue->GetNPackets()<<'\n';
                return;
            }
        }
        if (injectAck) {
            WireHeader h; p->PeekHeader(h);
            if (h.ack && !h.reverse && injectedAckLosses.erase(h.sequence)) {
                ++injectedAckCount; ++bulk->ackDrops;
                if (detail) {
                    const uint32_t bytes=ACK_BYTES+(sackOptionBytes?8*h.count:0);
                    linkLog<<Now()<<",1,injected-ack-drop,0,1,"<<h.sequence<<','<<bytes<<','<<reverseQueue->GetNPackets();
                    if (sackOptionBytes) linkLog<<','<<int(h.count);
                    linkLog<<'\n';
                }
                return;
            }
        }
        out->Send(p->Copy(),Address(),protocol);
    }
};
struct Burster {
    Flow* flow;
    Ptr<ExponentialRandomVariable> intervals;
    void Next() {
        burstLog<<Now()<<','<<tinyBurstBytes<<'\n';
        flow->Offer(tinyBurstBytes/DATA_BYTES);
        Simulator::Schedule(Seconds(intervals->GetValue()),&Burster::Next,this);
    }
};
}
int main(int argc,char** argv) {
    CommandLine cmd(__FILE__);
    cmd.AddValue("algorithm","reno | reno-sack | fack",algorithm);
    cmd.AddValue("overdamping","paper section 4.4",config.overdamping);
    cmd.AddValue("rampdown","paper equations (3),(4), no extra pacing",config.rampdown);
    cmd.AddValue("packetWindow","whole-packet sending budget",config.packetWindow);
    cmd.AddValue("integerCut","shared congestion-cut rounding (false keeps real values)",config.integerCut);
    cmd.AddValue("sackStarvationFallback","optional reconstructed paper section 4.3 fallback (default false)",config.sackStarvationFallback);
    cmd.AddValue("queue","LBNL Q: waiting capacity Q-1",queueLimit);
    cmd.AddValue("extraWaitingSlots","sensitivity only",extraWaitingSlots);
    cmd.AddValue("stop","duration seconds",stopTime);
    cmd.AddValue("warmup","measurement starts here (seconds)",warmup);
    cmd.AddValue("ssthresh","initial threshold in segments",config.initialSsthresh);
    cmd.AddValue("initialCwnd","initial window in segments",config.initialCwnd);
    cmd.AddValue("totalSegments","finite transfer; 0 saturated",config.totalSegments);
    cmd.AddValue("extraDrops","original segments lost once in transit at r2",lossList);
    cmd.AddValue("extraRetransmissionDrops","first retransmitted copy of each listed segment lost once at r2",retransmissionLossList);
    cmd.AddValue("extraAckDrops","first bulk ACK with each listed cumulative value lost once at r1",ackLossList);
    cmd.AddValue("tinyBurstBytes","reverse persistent Reno application burst, 0 disables",tinyBurstBytes);
    cmd.AddValue("tinyRate","mean reverse bursts per second",tinyRate);
    cmd.AddValue("seed","common reverse-application random run",seed);
    cmd.AddValue("detail","full event CSV, false keeps link counters/bins",detail);
    cmd.AddValue("queueProbe","isolated DropTail source-rule microbenchmark, no TCP",queueProbe);
    cmd.AddValue("queueProbePackets","number of 1000 B probe packets",queueProbePackets);
    cmd.AddValue("initialRto","shared initial RTO, seconds",initialRto);
    cmd.AddValue("minRto","shared minimum RTO, seconds",minRto);
    cmd.AddValue("maxRto","shared maximum RTO, seconds",maxRto);
    cmd.AddValue("timerTick","shared timer granularity, seconds",timerTick);
    cmd.AddValue("sackOptionBytes","add eight wire bytes per SACK block",sackOptionBytes);
    cmd.AddValue("ns1TimestampTimer","partial historical ns1 timestamp/fixed-point timer profile",ns1TimestampTimer);
    cmd.AddValue("out","output directory",output);
    cmd.Parse(argc,argv);
    if (queueProbe) return fack1996::queueprobe::Run(queueLimit,queueProbePackets,output);
    NS_ABORT_MSG_IF(algorithm!="reno"&&algorithm!="reno-sack"&&algorithm!="fack","unsupported algorithm");
    NS_ABORT_MSG_IF(queueLimit<2||stopTime<=warmup||warmup<0||tinyRate<=0||tinyBurstBytes%DATA_BYTES,"invalid configuration");
    NS_ABORT_MSG_IF(!std::isfinite(timerTick)||timerTick<=0,"invalid timer tick");
    NS_ABORT_MSG_IF(ns1TimestampTimer && timerTick>3,"historical timer tick exceeds its initialization range");
    NS_ABORT_MSG_IF(!ns1TimestampTimer && (!std::isfinite(initialRto)||!std::isfinite(minRto)||!std::isfinite(maxRto)
        ||minRto<=0||initialRto<minRto||maxRto<initialRto||timerTick>minRto),"invalid shared timer configuration");
    config.algorithm=algorithm=="reno"?Algorithm::Reno:algorithm=="reno-sack"?Algorithm::RenoSack:Algorithm::Fack;
    auto parseLosses=[](const std::string& list,std::set<uint32_t>& target) {
        std::stringstream input(list); std::string item;
        while (std::getline(input,item,',')) target.insert(std::stoul(item));
    };
    parseLosses(lossList,injectedLosses); parseLosses(retransmissionLossList,injectedRetransmissionLosses);
    parseLosses(ackLossList,injectedAckLosses);
    std::filesystem::create_directories(output);
    if (detail) Open(linkLog,output,"link-events.csv",std::string("time_s,direction,event,reverse_flow,is_ack,segment,wire_bytes,waiting_packets")+(sackOptionBytes?",sack_blocks":""));
    bins.resize(size_t(std::ceil(stopTime)));
    NodeContainer nodes; nodes.Create(tinyBurstBytes?6:4);
    PointToPointHelper left,middle,right;
    left.SetDeviceAttribute("DataRate",StringValue("10Mbps")); left.SetChannelAttribute("Delay",StringValue("2ms"));
    right.SetDeviceAttribute("DataRate",StringValue("10Mbps")); right.SetChannelAttribute("Delay",StringValue("33ms"));
    middle.SetDeviceAttribute("DataRate",StringValue("1.5Mbps")); middle.SetChannelAttribute("Delay",StringValue("5ms"));
    left.SetQueue("ns3::DropTailQueue<Packet>","MaxSize",StringValue("1000p"));
    right.SetQueue("ns3::DropTailQueue<Packet>","MaxSize",StringValue("1000p"));

    middle.SetQueue("ns3::DropTailQueue<Packet>","MaxSize",StringValue(std::to_string(queueLimit-1+extraWaitingSlots)+"p"));
    auto a=left.Install(nodes.Get(0),nodes.Get(1)),b=middle.Install(nodes.Get(1),nodes.Get(2)),c=right.Install(nodes.Get(2),nodes.Get(3));
    forwardQueue=DynamicCast<PointToPointNetDevice>(b.Get(0))->GetQueue();
    reverseQueue=DynamicCast<PointToPointNetDevice>(b.Get(1))->GetQueue();
    Flow bulkFlow(config,false,output); bulk=&bulkFlow;
    bulk->senderDevice=a.Get(0); bulk->receiverDevice=c.Get(1);
    Forwarder f1{b.Get(0)},f2{c.Get(0),true},r1{a.Get(1),false,true},r2{b.Get(1)};
    auto registerForward=[&](uint32_t node,Forwarder& f,uint16_t proto,Ptr<NetDevice> device) {
        nodes.Get(node)->RegisterProtocolHandler(MakeCallback(&Forwarder::Receive,&f),proto,device);
    };
    nodes.Get(0)->RegisterProtocolHandler(MakeCallback(&Flow::ReceiveAck,bulk),BULK_PROTOCOL,a.Get(0));
    nodes.Get(3)->RegisterProtocolHandler(MakeCallback(&Flow::ReceiveData,bulk),BULK_PROTOCOL,c.Get(1));
    registerForward(1,f1,BULK_PROTOCOL,a.Get(1)); registerForward(2,f2,BULK_PROTOCOL,b.Get(1));
    registerForward(1,r1,BULK_PROTOCOL,b.Get(0)); registerForward(2,r2,BULK_PROTOCOL,c.Get(0));
    std::unique_ptr<Flow> tinyFlow;
    Forwarder tf1,tf2,tr1,tr2;
    Burster burster;
    if (tinyBurstBytes) {
        PointToPointHelper near=left;
        auto d=near.Install(nodes.Get(1),nodes.Get(4));
        near.SetChannelAttribute("Delay",StringValue("3ms")); auto e=near.Install(nodes.Get(2),nodes.Get(5));
        auto tinyConfig=config; tinyConfig.algorithm=Algorithm::Reno; tinyConfig.overdamping=tinyConfig.rampdown=false;
        tinyConfig.saturated=false; tinyConfig.totalSegments=0; tinyConfig.initialCwnd=1; tinyConfig.initialSsthresh=1000;
        tinyFlow=std::make_unique<Flow>(tinyConfig,true,output+"/reverse"); tiny=tinyFlow.get();
        tiny->senderDevice=e.Get(1); tiny->receiverDevice=d.Get(1);
        tf1={b.Get(1)};tf2={d.Get(0)};tr1={b.Get(0)};tr2={e.Get(0)};
        nodes.Get(5)->RegisterProtocolHandler(MakeCallback(&Flow::ReceiveAck,tiny),TINY_PROTOCOL,e.Get(1));
        nodes.Get(4)->RegisterProtocolHandler(MakeCallback(&Flow::ReceiveData,tiny),TINY_PROTOCOL,d.Get(1));
        registerForward(2,tf1,TINY_PROTOCOL,e.Get(0)); registerForward(1,tf2,TINY_PROTOCOL,b.Get(0));
        registerForward(1,tr1,TINY_PROTOCOL,d.Get(0)); registerForward(2,tr2,TINY_PROTOCOL,b.Get(1));
        RngSeedManager::SetSeed(1); RngSeedManager::SetRun(seed);
        burster.flow=tiny; burster.intervals=CreateObject<ExponentialRandomVariable>();
        burster.intervals->SetAttribute("Mean",DoubleValue(1/tinyRate)); burster.intervals->SetStream(1);
        Open(burstLog,output,"bursts.csv","time_s,offered_bytes");
        Simulator::ScheduleNow(&Flow::Start,tiny);
        Simulator::Schedule(Seconds(burster.intervals->GetValue()),&Burster::Next,&burster);
    }
    for (uint32_t dir=0;dir<2;++dir) {
        auto q=dir?reverseQueue:forwardQueue;
        for (const auto& entry:std::vector<std::pair<std::string,std::string>>{{"Enqueue","enqueue"},{"Dequeue","dequeue"},{"Drop","drop"}})
            q->TraceConnectWithoutContext(entry.first,MakeBoundCallback(&QueueEvent,dir,entry.second));
    }
    Simulator::ScheduleNow(&Flow::Start,bulk);
    Simulator::Stop(Seconds(stopTime)); Simulator::Run();
    double forwardUtil=forwardBusy/(stopTime-warmup),reverseUtil=reverseDataBusy/(stopTime-warmup);
    std::ofstream binFile; Open(binFile,output,"link-busy.csv","start_s,duration_s,forward_busy_s,reverse_busy_s,bulk_data_s,tiny_data_s,delivered_segments,unique_received_segments,tiny_delivered_segments,tiny_unique_received_segments");
    for (size_t i=0;i<bins.size();++i) {
        double duration=std::min(1.0,stopTime-double(i));const auto& x=bins[i];
        NS_ABORT_MSG_IF(x.forward>duration+1e-8||x.reverse>duration+1e-8,"link busy time exceeds capacity");
        binFile<<i<<','<<duration<<','<<x.forward<<','<<x.reverse<<','<<x.bulk<<','<<x.tiny<<','<<x.delivered<<','<<x.received<<','<<x.tinyDelivered<<','<<x.tinyReceived<<'\n';
    }
    bulk->Summary(forwardUtil,reverseUtil); if (tiny) tiny->Summary(forwardUtil,reverseUtil);
    std::cout<<"measured forward busy="<<100*forwardUtil<<"% reverse tiny data="<<100*reverseUtil<<"%\n";
    Simulator::Destroy();
}
