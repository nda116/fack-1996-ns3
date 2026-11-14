#ifndef FACK_QUEUE_PROBE_H
#define FACK_QUEUE_PROBE_H

#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <vector>

namespace fack1996::queueprobe {
class SequenceTag : public ns3::Tag {
  public:
    uint32_t sequence=0;
    static ns3::TypeId GetTypeId() {
        static ns3::TypeId t=ns3::TypeId("ns3::FackQueueProbeTag")
            .SetParent<ns3::Tag>().AddConstructor<SequenceTag>();
        return t;
    }
    ns3::TypeId GetInstanceTypeId() const override { return GetTypeId(); }
    uint32_t GetSerializedSize() const override { return 4; }
    void Serialize(ns3::TagBuffer b) const override { b.WriteU32(sequence); }
    void Deserialize(ns3::TagBuffer b) override { sequence=b.ReadU32(); }
    void Print(std::ostream& o) const override { o<<sequence; }
};

struct Probe {
    ns3::Ptr<ns3::NetDevice> out;
    ns3::Ptr<ns3::Queue<ns3::Packet>> queue;
    std::ofstream log;
    uint32_t q=0,arrivals=0,received=0,drops=0,expectedDrops=0;
    int64_t serviceNs=0,completionNs=0;
    bool busy=false;
    std::deque<uint32_t> waiting;
    std::vector<uint32_t> actualLost,referenceLost;
    uint32_t Seq(ns3::Ptr<const ns3::Packet> p) {
        SequenceTag tag; NS_ABORT_MSG_IF(!p->PeekPacketTag(tag),"probe packet has no sequence");
        return tag.sequence;
    }
    bool ReferenceAccept(uint32_t sequence) {
        const auto now=ns3::Simulator::Now().GetNanoSeconds();
        while (busy && completionNs<=now) {
            if (waiting.empty()) busy=false;
            else { waiting.pop_front(); completionNs+=serviceNs; }
        }

        if (waiting.size()+1>=q) { ++expectedDrops; referenceLost.push_back(sequence); return false; }
        waiting.push_back(sequence);
        if (!busy) { waiting.pop_front(); busy=true; completionNs=now+serviceNs; }
        return true;
    }
    void Row(const char* event,uint32_t sequence,int expected=-1) {
        log<<ns3::Simulator::Now().GetSeconds()<<','<<event<<','<<sequence<<','
           <<queue->GetNPackets()<<','<<expected<<'\n';
    }
    void Arrival(ns3::Ptr<ns3::NetDevice>,ns3::Ptr<const ns3::Packet> p,uint16_t protocol,
                 const ns3::Address&,const ns3::Address&,ns3::NetDevice::PacketType) {
        ++arrivals; const auto sequence=Seq(p);
        Row("arrival",sequence,ReferenceAccept(sequence));
        out->Send(p->Copy(),ns3::Address(),protocol);
    }
    void Receive(ns3::Ptr<ns3::NetDevice>,ns3::Ptr<const ns3::Packet>,uint16_t,
                 const ns3::Address&,const ns3::Address&,ns3::NetDevice::PacketType) { ++received; }
    void Dequeue(ns3::Ptr<const ns3::Packet> p) { Row("dequeue",Seq(p)); }
    void Drop(ns3::Ptr<const ns3::Packet> p) {
        ++drops; actualLost.push_back(Seq(p)); Row("drop",Seq(p));
    }
};

inline int Run(uint32_t q,uint32_t packets,const std::string& output) {
    using namespace ns3;
    NS_ABORT_MSG_IF(q<2 || packets<1 || packets>10000,"invalid queue-probe configuration");
    std::filesystem::create_directories(output);
    Probe probe; probe.q=q; probe.serviceNs=DataRate("1.5Mbps").CalculateBytesTxTime(1000).GetNanoSeconds();
    probe.log.open(output+"/queue-probe.csv"); NS_ABORT_MSG_IF(!probe.log,"cannot open probe trace");
    probe.log<<std::fixed<<std::setprecision(9)<<"time_s,event,segment,waiting_packets,expected_accept\n";
    NodeContainer nodes; nodes.Create(3);
    PointToPointHelper access,bottleneck;
    access.SetDeviceAttribute("DataRate",StringValue("10Mbps"));
    access.SetChannelAttribute("Delay",StringValue("2ms"));
    access.SetQueue("ns3::DropTailQueue<Packet>","MaxSize",StringValue("10000p"));
    bottleneck.SetDeviceAttribute("DataRate",StringValue("1.5Mbps"));
    bottleneck.SetChannelAttribute("Delay",StringValue("5ms"));
    bottleneck.SetQueue("ns3::DropTailQueue<Packet>","MaxSize",StringValue(std::to_string(q-1)+"p"));
    auto a=access.Install(nodes.Get(0),nodes.Get(1)),b=bottleneck.Install(nodes.Get(1),nodes.Get(2));
    probe.out=b.Get(0); probe.queue=DynamicCast<PointToPointNetDevice>(b.Get(0))->GetQueue();
    nodes.Get(1)->RegisterProtocolHandler(MakeCallback(&Probe::Arrival,&probe),0x0800,a.Get(1));
    nodes.Get(2)->RegisterProtocolHandler(MakeCallback(&Probe::Receive,&probe),0x0800,b.Get(1));
    probe.queue->TraceConnectWithoutContext("Dequeue",MakeCallback(&Probe::Dequeue,&probe));
    probe.queue->TraceConnectWithoutContext("Drop",MakeCallback(&Probe::Drop,&probe));
    for (uint32_t i=0;i<packets;++i) {
        auto packet=Create<Packet>(998); SequenceTag tag; tag.sequence=i; packet->AddPacketTag(tag);
        NS_ABORT_MSG_IF(!a.Get(0)->Send(packet,Address(),0x0800),"probe source queue overflow");
    }
    Simulator::Stop(Seconds(1)+NanoSeconds((packets+1)*probe.serviceNs)); Simulator::Run();
    NS_ABORT_MSG_IF(probe.arrivals!=packets || probe.received+probe.drops!=packets,"probe did not drain");
    NS_ABORT_MSG_IF(probe.actualLost!=probe.referenceLost,"ns-3 queue differs from translated ns-1 source rules");
    std::ofstream summary(output+"/summary.json");
    summary<<"{\n\"mode\":\"queue_probe\",\n\"queue\":"<<q<<",\n\"waiting_capacity\":"<<q-1
           <<",\n\"queue_probe_packets\":"<<packets<<",\n\"arrivals\":"<<probe.arrivals
           <<",\n\"received\":"<<probe.received<<",\n\"drops\":"<<probe.drops
           <<",\n\"expected_drops\":"<<probe.expectedDrops<<",\n\"service_ns\":"<<probe.serviceNs
           <<",\n\"arrival_spacing_ns\":800000,\n\"model_packet_bytes\":1000\n}\n";
    std::cout<<"PASS queue probe Q="<<q<<" packets="<<packets<<" drops="<<probe.drops<<'\n';
    Simulator::Destroy(); return 0;
}
}
#endif
