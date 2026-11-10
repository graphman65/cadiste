// Ideal gearing: physical output torque is applied exclusively by JointController.
#include <algorithm>
#include <cmath>
#include <limits>
#include <memory>
#include <string>
#include <vector>

#include <gz/common/Console.hh>
#include <gz/math/Pose3.hh>
#include <gz/math/Quaternion.hh>
#include <gz/math/Vector3.hh>
#include <gz/msgs/double.pb.h>
#include <gz/plugin/Register.hh>
#include <gz/sim/EntityComponentManager.hh>
#include <gz/sim/Joint.hh>
#include <gz/sim/Model.hh>
#include <gz/sim/System.hh>
#include <gz/sim/components/JointPosition.hh>
#include <gz/sim/components/Name.hh>
#include <gz/sim/components/ParentEntity.hh>
#include <gz/sim/components/Pose.hh>
#include <gz/sim/components/Visual.hh>
#include <gz/transport/Node.hh>
#include <sdf/Element.hh>

namespace cadiste
{
class IdealTransmission final : public gz::sim::System,
                                public gz::sim::ISystemConfigure,
                                public gz::sim::ISystemPreUpdate
{
  struct Relay
  {
    double ratio;
    double velocityLimit;
    gz::transport::Node::Publisher publisher;

    void Command(const gz::msgs::Double &input)
    {
      // NaN/Inf must never reach the physical controller. Saturation is in
      // public motor units, before the signed output-coordinate conversion.
      if (!std::isfinite(input.data()))
        return;
      gz::msgs::Double output;
      output.set_data(ratio * std::clamp(input.data(), -velocityLimit, velocityLimit));
      publisher.Publish(output);
    }
  };

  struct Leaf
  {
    std::string name;
    gz::math::Pose3d offset;
    gz::sim::Entity entity{gz::sim::kNullEntity};
  };

  struct Visual
  {
    std::string parentName;
    std::string jointName;
    gz::math::Pose3d pivot;
    gz::math::Vector3d axis;
    double multiplier;
    double lastPosition{std::numeric_limits<double>::quiet_NaN()};
    gz::sim::Entity parent{gz::sim::kNullEntity};
    gz::sim::Entity joint{gz::sim::kNullEntity};
    std::vector<Leaf> leaves;
  };

  public: void Configure(const gz::sim::Entity &entity,
                         const std::shared_ptr<const sdf::Element> &sdf,
                         gz::sim::EntityComponentManager &ecm,
                         gz::sim::EventManager &) override
  {
    model = gz::sim::Model(entity);
    if (!model.Valid(ecm))
    {
      gzerr << "IdealTransmission must be attached to a model.\n";
      return;
    }
    if (sdf->HasElement("transmission"))
    {
      for (auto entry = sdf->GetElementImpl("transmission"); entry;
           entry = entry->GetNextElement("transmission"))
      {
        auto relay = std::make_unique<Relay>();
        relay->ratio = entry->Get<double>("ratio");
        relay->velocityLimit = entry->Get<double>("velocity_limit");
        if (!std::isfinite(relay->ratio) || relay->ratio == 0 ||
            !std::isfinite(relay->velocityLimit) || relay->velocityLimit <= 0 ||
            !std::isfinite(relay->ratio * relay->velocityLimit))
        {
          gzerr << "IdealTransmission has invalid ratio / motor velocity limit.\n";
          continue;
        }
        relay->publisher = node.Advertise<gz::msgs::Double>(entry->Get<std::string>("output_topic"));
        Relay *target = relay.get();
        const std::string topic = entry->Get<std::string>("input_topic");
        if (!relay->publisher || !node.Subscribe(topic, &Relay::Command, target))
        {
          gzerr << "IdealTransmission could not connect motor topic [" << topic << "].\n";
          continue;
        }
        relays.push_back(std::move(relay));
      }
    }
    if (sdf->HasElement("driven_visual"))
    {
      for (auto entry = sdf->GetElementImpl("driven_visual"); entry;
           entry = entry->GetNextElement("driven_visual"))
      {
        Visual visual;
        visual.parentName = entry->Get<std::string>("parent");
        visual.jointName = entry->Get<std::string>("joint");
        visual.pivot = entry->Get<gz::math::Pose3d>("pose");
        visual.axis = entry->Get<gz::math::Vector3d>("axis");
        visual.multiplier = entry->Get<double>("multiplier");
        if (!visual.axis.IsFinite() ||
            std::abs(visual.axis.Length() - 1.0) > 1e-9 ||
            !std::isfinite(visual.multiplier))
        {
          gzerr << "IdealTransmission has invalid visual axis / multiplier.\n";
          continue;
        }
        if (entry->HasElement("leaf"))
        {
          for (auto leaf = entry->GetElement("leaf"); leaf;
               leaf = leaf->GetNextElement("leaf"))
          {
            visual.leaves.push_back({leaf->Get<std::string>("name"),
                                     leaf->Get<gz::math::Pose3d>("pose")});
          }
        }
        visuals.push_back(std::move(visual));
      }
    }
    Resolve(ecm);
  }

  public: void PreUpdate(const gz::sim::UpdateInfo &,
                         gz::sim::EntityComponentManager &ecm) override
  {
    // Entities may not yet exist during Configure. Resolve once, then all hot
    // paths use cached entities and preallocated leaf records.
    if (!resolved)
      Resolve(ecm);
    for (auto &visual : visuals)
    {
      if (visual.joint == gz::sim::kNullEntity)
        continue;
      const auto *position = ecm.Component<gz::sim::components::JointPosition>(visual.joint);
      if (!position || position->Data().empty() || !std::isfinite(position->Data()[0]))
        continue;
      if (position->Data()[0] == visual.lastPosition)
        continue;
      const double angle = position->Data()[0] * visual.multiplier;
      if (!std::isfinite(angle))
        continue;
      const gz::math::Pose3d spin(gz::math::Vector3d::Zero,
                                gz::math::Quaterniond(visual.axis, angle));
      const auto pivot = visual.pivot * spin;
      bool allLeavesReady = true;
      for (const auto &leaf : visual.leaves)
      {
        auto *pose = ecm.Component<gz::sim::components::Pose>(leaf.entity);
        if (!pose)
        {
          allLeavesReady = false;
          continue;
        }
        if (pose->SetData(pivot * leaf.offset,
            [](const auto &a, const auto &b) { return a == b; }))
        {
          ecm.SetChanged(leaf.entity, gz::sim::components::Pose::typeId,
                         gz::sim::ComponentState::OneTimeChange);
        }
      }
      if (allLeavesReady)
        visual.lastPosition = position->Data()[0];
    }
  }

  private: void Resolve(gz::sim::EntityComponentManager &ecm)
  {
    resolved = true;
    for (auto &visual : visuals)
    {
      if (visual.parent == gz::sim::kNullEntity)
        visual.parent = model.LinkByName(ecm, visual.parentName);
      if (visual.joint == gz::sim::kNullEntity)
      {
        visual.joint = model.JointByName(ecm, visual.jointName);
        if (visual.joint != gz::sim::kNullEntity)
          gz::sim::Joint(visual.joint).EnablePositionCheck(ecm);
      }
      if (visual.parent == gz::sim::kNullEntity || visual.joint == gz::sim::kNullEntity)
      {
        resolved = false;
        continue;
      }
      for (auto &leaf : visual.leaves)
      {
        if (leaf.entity == gz::sim::kNullEntity)
          leaf.entity = ecm.EntityByComponents(
              gz::sim::components::Visual(), gz::sim::components::Name(leaf.name),
              gz::sim::components::ParentEntity(visual.parent));
        if (leaf.entity == gz::sim::kNullEntity)
          resolved = false;
      }
    }
  }

  private: gz::sim::Model model{gz::sim::kNullEntity};
  private: bool resolved{false};
  private: std::vector<std::unique_ptr<Relay>> relays;
  private: std::vector<Visual> visuals;
  // Destroy the transport node (and its callbacks) before their relay storage.
  private: gz::transport::Node node;
};
}

GZ_ADD_PLUGIN(cadiste::IdealTransmission, gz::sim::System,
              cadiste::IdealTransmission::ISystemConfigure,
              cadiste::IdealTransmission::ISystemPreUpdate)
GZ_ADD_PLUGIN_ALIAS(cadiste::IdealTransmission, "cadiste::IdealTransmission")
