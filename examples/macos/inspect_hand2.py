#!/usr/bin/env python3
"""Read Hand 2 metadata and joint states through the installed wuji_sdk package.

Use automatic discovery, or pass --address IP:50001 / --sn SERIAL.
This script sends no motor, origin, gain, or target commands.
"""
import argparse
import asyncio

from wuji_sdk import ConnectOptions, DeviceType, SdkManager, WujiHand2


async def inspect(args):
    manager = SdkManager.instance()
    selector = {"address": args.address} if args.address else {"sn": args.sn}
    if not any(selector.values()):
        devices = manager.scan()
        for device in devices:
            print(f"Discovered {device.sn}: {device.device_type}, {device.address}")
        hands = [device for device in devices if device.device_type == DeviceType.WujiHand2]
        if len(hands) != 1:
            print(f"Found {len(hands)} Hand 2 devices; select one with --address or --sn.")
            return
        selector = {"sn": hands[0].sn}
    hand = manager.connect(**selector, device_name="inspect_hand2",
                           options=ConnectOptions(enable_bridge=False))
    subscription = None
    try:
        if not isinstance(hand, WujiHand2):
            raise TypeError(f"Selected device is {type(hand).__name__}, expected WujiHand2")
        print(f"Connected: {hand.serial_number}")
        print(f"Device info: {hand.info}")
        print(f"Topics: {', '.join(topic.path for topic in hand.topics())}")
        if args.frames:
            subscription = hand.joint_states().subscribe()
            for _ in range(args.frames):
                frame = await asyncio.wait_for(subscription.recv_async(), args.timeout)
                positions = ", ".join(f"{joint.nid}:{joint.position:.4f}" for joint in frame.joints)
                print(f"seq={frame.header.seq} timestamp_us={frame.header.timestamp_us} "
                      f"joints={frame.num_joints} positions=[{positions}]")
    finally:
        if subscription is not None:
            subscription.close()
        hand.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--address", help="Hand 2 UDP address, IP:50001")
    selector.add_argument("--sn", help="Hand 2 serial number")
    parser.add_argument("--frames", type=int, default=5, help="Joint frames to read; 0 reads metadata only")
    parser.add_argument("--timeout", type=float, default=5.0, help="Timeout in seconds for each frame")
    args = parser.parse_args()
    if args.frames < 0 or not 0 < args.timeout < float("inf"):
        parser.error("--frames must be nonnegative and --timeout must be finite and positive")
    try:
        asyncio.run(inspect(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
