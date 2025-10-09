# -*- coding: utf-8 -*-
"""
BLE Client for python-for-android
"""
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    if sys.platform != "android":
        assert False, "This backend is only available on Android"

import asyncio
import logging
import uuid
import warnings
from typing import Any, Optional, Union

if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override

from android.broadcast import BroadcastReceiver
from jnius import java_method

from bleak.assigned_numbers import gatt_char_props_to_strs
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient, NotifyCallback
from bleak.backends.descriptor import BleakGATTDescriptor
from bleak.backends.device import BLEDevice
from bleak.backends.p4android import defs, utils
from bleak.backends.service import BleakGATTService, BleakGATTServiceCollection
from bleak.exc import BleakError

logger = logging.getLogger(__name__)


class BleakClientP4Android(BaseBleakClient):
    """A python-for-android Bleak Client

    Args:
        address_or_ble_device:
            The Bluetooth address of the BLE peripheral to connect to or the
            :class:`BLEDevice` object representing it.
        services:
            Optional set of services UUIDs to filter.
    """

    def __init__(
        self,
        address_or_ble_device: Union[BLEDevice, str],
        services: Optional[set[uuid.UUID]] = None,
        **kwargs,
    ):
        super(BleakClientP4Android, self).__init__(address_or_ble_device, **kwargs)
        self._requested_services = (
            set(map(defs.UUID.fromString, services)) if services else None
        )
        # kwarg "device" is for backwards compatibility
        self.__adapter = kwargs.get("adapter", kwargs.get("device", None))
        self.__gatt = None
        self.__device = None
        self.__mtu = 23
        self.__callbacks = None
        self._subscriptions = {}
        self._is_connecting = False
        self._is_disconnecting = False

    def _reset_state(self):
        """Reset internal state for clean reconnection"""
        logger.debug("Resetting client state")
        self.__gatt = None
        self.__device = None
        self.__callbacks = None
        self._subscriptions = {}
        self.__mtu = 23
        self.services = None
        self._is_connecting = False
        self._is_disconnecting = False

    # Connectivity methods

    @override
    async def connect(self, pair: bool = False, **kwargs) -> None:
        """Connect to the specified GATT server."""
        if self._is_connecting:
            raise BleakError("Connection already in progress")

        if self.is_connected:
            logger.debug("Already connected")
            return

        self._is_connecting = True

        try:
            if pair:
                logger.warning("Pairing during connect is not implemented on Android")

            loop = asyncio.get_running_loop()

            # Clean state before connecting
            self._reset_state()
            self._is_connecting = True

            self.__adapter = defs.BluetoothAdapter.getDefaultAdapter()
            if self.__adapter is None:
                raise BleakError("Bluetooth is not supported on this hardware platform")
            if self.__adapter.getState() != defs.BluetoothAdapter.STATE_ON:
                raise BleakError("Bluetooth is not turned on")

            self.__device = self.__adapter.getRemoteDevice(self.address)
            self.__callbacks = _PythonBluetoothGattCallback(self, loop)

            logger.debug(f"Connecting to BLE device @ {self.address}")

            # Add delay before connection attempt to ensure clean state
            # await asyncio.sleep(0.2)

            (self.__gatt,) = await self.__callbacks.perform_and_wait(
                dispatchApi=self.__device.connectGatt,
                dispatchParams=(
                    defs.context,
                    False,
                    self.__callbacks.java,
                    defs.BluetoothDevice.TRANSPORT_LE,
                ),
                resultApi="onConnectionStateChange",
                resultExpected=(defs.BluetoothProfile.STATE_CONNECTED,),
                return_indicates_status=False,
            )

            logger.debug("Connection successful.")

            # Add stability delay after connection
            # await asyncio.sleep(0.3)

            # unlike other backends, Android doesn't automatically negotiate
            # the MTU, so we request the largest size possible like BlueZ
            logger.debug("requesting mtu...")
            try:
                (self.__mtu,) = await self.__callbacks.perform_and_wait(
                    dispatchApi=self.__gatt.requestMtu,
                    dispatchParams=(517,),
                    resultApi="onMtuChanged",
                )
            except Exception as e:
                logger.warning(f"MTU request failed, using default: {e}")
                self.__mtu = 23

            # Add delay after MTU negotiation
            # await asyncio.sleep(0.2)

            logger.debug("discovering services...")
            await self.__callbacks.perform_and_wait(
                dispatchApi=self.__gatt.discoverServices,
                dispatchParams=(),
                resultApi="onServicesDiscovered",
            )

            # Add delay after service discovery
            # await asyncio.sleep(0.2)

            await self._get_services()

            # Final stability delay
            # await asyncio.sleep(0.1)
            logger.debug("Connection stabilized")

        except BaseException as e:
            logger.error(f"Connection failed: {e}")
            # if connecting is canceled or one of the above fails, we need to
            # disconnect and clean up
            try:
                await self._force_disconnect()
            except Exception as cleanup_error:
                logger.error(f"Cleanup after failed connection failed: {cleanup_error}")
            raise
        finally:
            self._is_connecting = False

    async def _force_disconnect(self):
        """Force disconnect and cleanup without waiting for callbacks"""
        logger.debug("Force disconnecting...")

        if self.__gatt is not None:
            try:
                self.__gatt.disconnect()
                # await asyncio.sleep(0.1)
                self.__gatt.close()
                # await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning(f"Error during force disconnect: {e}")

        self._reset_state()

    @override
    async def disconnect(self) -> None:
        """Disconnect from the specified GATT server."""
        if self._is_disconnecting:
            logger.debug("Disconnect already in progress")
            return

        if self.__gatt is None:
            logger.debug("Already disconnected")
            return

        self._is_disconnecting = True

        try:
            logger.debug("Disconnecting from BLE device...")

            # Clear all subscriptions first
            if hasattr(self, "_subscriptions"):
                for handle in list(self._subscriptions.keys()):
                    try:
                        characteristic = self.services.get_characteristic(handle)
                        if characteristic:
                            await self.stop_notify(characteristic)
                    except Exception as e:
                        logger.warning(
                            f"Error stopping notification during disconnect: {e}"
                        )

            # Add delay before disconnection
            # await asyncio.sleep(0.1)

            # Try to disconnect gracefully
            try:
                await self.__callbacks.perform_and_wait(
                    dispatchApi=self.__gatt.disconnect,
                    dispatchParams=(),
                    resultApi="onConnectionStateChange",
                    resultExpected=(defs.BluetoothProfile.STATE_DISCONNECTED,),
                    unless_already=True,
                    return_indicates_status=False,
                )

                # Add delay after disconnect
                # await asyncio.sleep(0.2)

                self.__gatt.close()

                # Add delay after close
                # await asyncio.sleep(0.2)

            except Exception as e:
                logger.error(f"Graceful disconnect failed: {e}")
                # Force cleanup
                try:
                    self.__gatt.disconnect()
                    # await asyncio.sleep(0.1)
                    self.__gatt.close()
                    # await asyncio.sleep(0.1)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Disconnect failed: {e}")
        finally:
            # Always reset state
            self._reset_state()
            logger.debug("Disconnect completed")

    @override
    async def pair(self, *args, **kwargs) -> None:
        """Pair with the peripheral.

        You can use ConnectDevice method if you already know the MAC address of the device.
        Else you need to StartDiscovery, Trust, Pair and Connect in sequence.
        """
        loop = asyncio.get_running_loop()

        bondedFuture = loop.create_future()

        def handleBondStateChanged(context, intent):
            bond_state = intent.getIntExtra(defs.BluetoothDevice.EXTRA_BOND_STATE, -1)
            if bond_state == -1:
                loop.call_soon_threadsafe(
                    bondedFuture.set_exception,
                    BleakError(f"Unexpected bond state {bond_state}"),
                )
            elif bond_state == defs.BluetoothDevice.BOND_NONE:
                loop.call_soon_threadsafe(
                    bondedFuture.set_exception,
                    BleakError(
                        f"Device with address {self.address} could not be paired with."
                    ),
                )
            elif bond_state == defs.BluetoothDevice.BOND_BONDED:
                loop.call_soon_threadsafe(bondedFuture.set_result, True)

        receiver = BroadcastReceiver(
            handleBondStateChanged,
            actions=[defs.BluetoothDevice.ACTION_BOND_STATE_CHANGED],
        )
        receiver.start()
        try:
            # See if it is already paired.
            bond_state = self.__device.getBondState()
            if bond_state == defs.BluetoothDevice.BOND_BONDED:
                return
            elif bond_state == defs.BluetoothDevice.BOND_NONE:
                logger.debug(f"Pairing to BLE device @ {self.address}")
                if not self.__device.createBond():
                    raise BleakError(
                        f"Could not initiate bonding with device @ {self.address}"
                    )
            await bondedFuture
        finally:
            await receiver.stop()

    @override
    async def unpair(self) -> None:
        """Unpair with the peripheral."""
        warnings.warn(
            "Unpairing is seemingly unavailable in the Android API at the moment."
        )

    @property
    @override
    def is_connected(self) -> bool:
        """Check connection status between this client and the server.

        Returns:
            Boolean representing connection status.

        """
        return (
            self.__callbacks is not None
            and self.__gatt is not None
            and self.__callbacks.states.get("onConnectionStateChange", [None, None])[1]
            == defs.BluetoothProfile.STATE_CONNECTED
        )

    @property
    @override
    def mtu_size(self) -> int:
        return self.__mtu

    # GATT services methods

    async def _get_services(self) -> BleakGATTServiceCollection:
        """Get all services registered for this GATT server.

        Returns:
           A :py:class:`bleak.backends.service.BleakGATTServiceCollection` with this device's services tree.

        """
        if self.services is not None:
            return self.services

        services = BleakGATTServiceCollection()

        logger.debug("Get Services...")
        for java_service in self.__gatt.getServices():
            if (
                self._requested_services is not None
                and java_service.getUuid() not in self._requested_services
            ):
                continue

            service = BleakGATTService(
                java_service,
                java_service.getInstanceId(),
                java_service.getUuid().toString(),
            )
            services.add_service(service)

            for java_characteristic in java_service.getCharacteristics():

                characteristic = BleakGATTCharacteristic(
                    java_characteristic,
                    java_characteristic.getInstanceId(),
                    java_characteristic.getUuid().toString(),
                    gatt_char_props_to_strs((java_characteristic.getProperties())),
                    lambda: self.__mtu - 3,
                    service,
                )
                services.add_characteristic(characteristic)

                for descriptor_index, java_descriptor in enumerate(
                    java_characteristic.getDescriptors()
                ):

                    descriptor = BleakGATTDescriptor(
                        java_descriptor,
                        characteristic.handle + 1 + descriptor_index,
                        java_descriptor.getUuid().toString(),
                        characteristic,
                    )
                    services.add_descriptor(descriptor)

        self.services = services
        return self.services

    async def _ensure_connection_stable(self, delay: float = 0.1) -> None:
        """Ensure connection is stable before operations"""
        if not self.is_connected:
            raise BleakError("Device not connected")

        # await asyncio.sleep(delay)

        # Double check connection
        if not self.is_connected:
            raise BleakError("Connection lost during stability check")

    # IO methods

    @override
    async def read_gatt_char(
        self, characteristic: BleakGATTCharacteristic, **kwargs: Any
    ) -> bytearray:
        """Perform read operation on the specified GATT characteristic with retry logic.

        Args:
            characteristic (BleakGATTCharacteristic): The characteristic to read from.
            max_retries: Optional number of retries (default: 3).
            retry_delay: Optional delay in seconds between retries (default: 0.1).

        Returns:
            (bytearray) The read data.

        """
        max_retries = kwargs.get("max_retries", 3)
        retry_delay = kwargs.get("retry_delay", 0.1)

        last_exception = None

        for attempt in range(max_retries):
            try:
                # Ensure connection is stable before reading
                await self._ensure_connection_stable()

                logger.debug(
                    f"Reading characteristic {characteristic.uuid}, attempt {attempt + 1}"
                )

                # Add small delay for subsequent attempts
                if attempt > 0:
                    wait_time = retry_delay * (
                        2 ** (attempt - 1)
                    )  # Exponential backoff
                    logger.debug(f"Waiting {wait_time:.2f}s before retry...")
                    # await asyncio.sleep(wait_time)

                (value,) = await self.__callbacks.perform_and_wait(
                    dispatchApi=self.__gatt.readCharacteristic,
                    dispatchParams=(characteristic.obj,),
                    resultApi=("onCharacteristicRead", characteristic.handle),
                )
                value = bytearray(value)
                logger.debug(
                    f"Read Characteristic {characteristic.uuid} | {characteristic.handle}: {len(value)} bytes"
                )
                return value

            except Exception as e:
                last_exception = e
                logger.warning(
                    f"Read attempt {attempt + 1} failed for {characteristic.uuid}: {e}"
                )

                if attempt < max_retries - 1:
                    # Check if connection is still alive
                    if not self.is_connected:
                        raise BleakError("Connection lost during read operation")
                    continue
                else:
                    logger.error(
                        f"All {max_retries} read attempts failed for {characteristic.uuid}"
                    )
                    break

        raise BleakError(
            f"Failed to read characteristic {characteristic.uuid} after {max_retries} attempts: {last_exception}"
        )

    @override
    async def read_gatt_descriptor(
        self, descriptor: BleakGATTDescriptor, **kwargs: Any
    ) -> bytearray:
        """Perform read operation on the specified GATT descriptor with retry logic.

        Args:
            descriptor: The descriptor to read from.
            max_retries: Optional number of retries (default: 3).
            retry_delay: Optional delay in seconds between retries (default: 0.1).

        Returns:
            The read data.
        """
        max_retries = kwargs.get("max_retries", 3)
        retry_delay = kwargs.get("retry_delay", 0.1)

        last_exception = None

        for attempt in range(max_retries):
            try:
                await self._ensure_connection_stable()

                logger.debug(
                    f"Reading descriptor {descriptor.uuid}, attempt {attempt + 1}"
                )

                if attempt > 0:
                    wait_time = retry_delay * (2 ** (attempt - 1))
                    # await asyncio.sleep(wait_time)

                (value,) = await self.__callbacks.perform_and_wait(
                    dispatchApi=self.__gatt.readDescriptor,
                    dispatchParams=(descriptor.obj,),
                    resultApi=("onDescriptorRead", descriptor.uuid),
                )
                value = bytearray(value)

                logger.debug(
                    f"Read Descriptor {descriptor.uuid} | {descriptor.handle}: {len(value)} bytes"
                )

                return value

            except Exception as e:
                last_exception = e
                logger.warning(f"Descriptor read attempt {attempt + 1} failed: {e}")

                if attempt < max_retries - 1:
                    if not self.is_connected:
                        raise BleakError("Connection lost during descriptor read")
                    continue
                else:
                    break

        raise BleakError(
            f"Failed to read descriptor {descriptor.uuid} after {max_retries} attempts: {last_exception}"
        )

    @override
    async def write_gatt_char(
        self, characteristic: BleakGATTCharacteristic, data: bytearray, response: bool
    ) -> None:
        # Ensure connection is stable before write
        await self._ensure_connection_stable(0.05)

        if response:
            characteristic.obj.setWriteType(
                defs.BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            )
        else:
            characteristic.obj.setWriteType(
                defs.BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
            )

        characteristic.obj.setValue(data)

        await self.__callbacks.perform_and_wait(
            dispatchApi=self.__gatt.writeCharacteristic,
            dispatchParams=(characteristic.obj,),
            resultApi=("onCharacteristicWrite", characteristic.handle),
        )

        logger.debug(
            f"Write Characteristic {characteristic.uuid} | {characteristic.handle}: {data}"
        )

    @override
    async def write_gatt_descriptor(
        self,
        desc_specifier: Union[BleakGATTDescriptor, str, uuid.UUID],
        data: bytearray,
    ) -> None:
        """Perform a write operation on the specified GATT descriptor.

        Args:
            desc_specifier (BleakGATTDescriptor, str or UUID): The descriptor to write
                to, specified by either UUID or directly by the
                BleakGATTDescriptor object representing it.
            data (bytes or bytearray): The data to send.

        """
        if not isinstance(desc_specifier, BleakGATTDescriptor):
            descriptor = self.services.get_descriptor(desc_specifier)
        else:
            descriptor = desc_specifier

        if not descriptor:
            raise BleakError(f"Descriptor {desc_specifier} was not found!")

        # Ensure connection is stable before write
        await self._ensure_connection_stable(0.05)

        descriptor.obj.setValue(data)

        await self.__callbacks.perform_and_wait(
            dispatchApi=self.__gatt.writeDescriptor,
            dispatchParams=(descriptor.obj,),
            resultApi=("onDescriptorWrite", descriptor.uuid),
        )

        logger.debug(
            f"Write Descriptor {descriptor.uuid} | {descriptor.handle}: {data}"
        )

    @override
    async def start_notify(
        self,
        characteristic: BleakGATTCharacteristic,
        callback: NotifyCallback,
        **kwargs,
    ) -> None:
        """
        Activate notifications/indications on a characteristic with retry logic.

        Args:
            characteristic: The descriptor to set notifications for.
            callback: notification callback.
            max_retries: Optional number of retries (default: 3).
            retry_delay: Optional delay in seconds between retries (default: 0.1).
        """
        max_retries = kwargs.get("max_retries", 3)
        retry_delay = kwargs.get("retry_delay", 0.2)

        last_exception = None

        for attempt in range(max_retries):
            try:
                await self._ensure_connection_stable(0.1)

                logger.debug(
                    f"Enabling notifications for {characteristic.uuid}, attempt {attempt + 1}"
                )

                if attempt > 0:
                    wait_time = retry_delay * attempt
                    # await asyncio.sleep(wait_time)

                if not self.__gatt.setCharacteristicNotification(
                    characteristic.obj, True
                ):
                    raise BleakError(
                        f"setCharacteristicNotification failed for {characteristic.uuid}"
                    )

                logger.debug("setCharacteristicNotification successful")

                # Do not overwhelm the backend
                # await asyncio.sleep(0.1)

                # Write CCCD descriptor
                cccd = characteristic.get_descriptor("2902")
                if not cccd:
                    raise BleakError("CCCD descriptor not found")

                cccd.obj.setValue(b"\x01\x00")

                await self.__callbacks.perform_and_wait(
                    dispatchApi=self.__gatt.writeDescriptor,
                    dispatchParams=(cccd.obj,),
                    resultApi=("onDescriptorWrite", cccd.uuid),
                )

                # Allow time for notification setup
                # await asyncio.sleep(0.1)

                # Register callback
                self._subscriptions[characteristic.handle] = callback

                logger.debug(f"Notifications enabled for {characteristic.uuid}")
                return

            except Exception as e:
                last_exception = e
                logger.warning(f"Notification setup attempt {attempt + 1} failed: {e}")

                # Cleanup on failure
                try:
                    if characteristic.handle in self._subscriptions:
                        del self._subscriptions[characteristic.handle]
                    self.__gatt.setCharacteristicNotification(characteristic.obj, False)
                except Exception:
                    pass

                if attempt < max_retries - 1:
                    if not self.is_connected:
                        raise BleakError("Connection lost during notification setup")
                    continue
                else:
                    break

        raise BleakError(
            f"Failed to enable notifications for {characteristic.uuid} after {max_retries} attempts: {last_exception}"
        )

    @override
    async def stop_notify(self, characteristic: BleakGATTCharacteristic) -> None:
        """Deactivate notification/indication on a specified characteristic.

        Args:
            characteristic (BleakGATTCharacteristic): The characteristic to deactivate
                notification/indication on,.

        """
        try:
            # Remove callback first
            if characteristic.handle in self._subscriptions:
                del self._subscriptions[characteristic.handle]

            # Write CCCD to disable notifications
            cccd = characteristic.get_descriptor("2902")
            if cccd:
                await self.write_gatt_descriptor(
                    cccd,
                    defs.BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE,
                )

            # Disable notification on GATT
            if self.__gatt and not self.__gatt.setCharacteristicNotification(
                characteristic.obj, False
            ):
                logger.warning(
                    f"Failed to disable notification for characteristic {characteristic.uuid}"
                )

            logger.debug(f"Notifications disabled for {characteristic.uuid}")

        except Exception as e:
            logger.error(f"Error stopping notifications for {characteristic.uuid}: {e}")
            # Always try to clean up subscription
            if characteristic.handle in self._subscriptions:
                del self._subscriptions[characteristic.handle]
            raise


class _PythonBluetoothGattCallback(utils.AsyncJavaCallbacks):
    __javainterfaces__ = [
        "com.github.hbldh.bleak.PythonBluetoothGattCallback$Interface"
    ]

    def __init__(self, client, loop):
        super().__init__(loop)
        self._client = client
        self.java = defs.PythonBluetoothGattCallback(self)

    def result_state(self, status, resultApi, *data):
        if status == defs.BluetoothGatt.GATT_SUCCESS:
            failure_str = None
        else:
            failure_str = defs.GATT_STATUS_STRINGS.get(status, status)
        self._loop.call_soon_threadsafe(
            self._result_state_unthreadsafe, failure_str, resultApi, data
        )

    @java_method("(II)V")
    def onConnectionStateChange(self, status, new_state):
        try:
            self.result_state(status, "onConnectionStateChange", new_state)
        except Exception as e:
            logger.warning(f"Error in onConnectionStateChange: {e}")

        if (
            new_state == defs.BluetoothProfile.STATE_DISCONNECTED
            and self._client._disconnected_callback is not None
        ):
            try:
                self._client._disconnected_callback()
            except Exception as e:
                logger.error(f"Error in disconnected callback: {e}")

    @java_method("(II)V")
    def onMtuChanged(self, mtu, status):
        self.result_state(status, "onMtuChanged", mtu)

    @java_method("(I)V")
    def onServicesDiscovered(self, status):
        self.result_state(status, "onServicesDiscovered")

    @java_method("(I[B)V")
    def onCharacteristicChanged(self, handle, value):
        try:
            if handle in self._client._subscriptions:
                self._loop.call_soon_threadsafe(
                    self._client._subscriptions[handle], bytearray(value.tolist())
                )
            else:
                logger.warning(
                    f"Received notification for unregistered handle {handle}"
                )
        except Exception as e:
            logger.error(f"Error in onCharacteristicChanged: {e}")

    @java_method("(II[B)V")
    def onCharacteristicRead(self, handle, status, value):
        self.result_state(
            status, ("onCharacteristicRead", handle), bytes(value.tolist())
        )

    @java_method("(II)V")
    def onCharacteristicWrite(self, handle, status):
        self.result_state(status, ("onCharacteristicWrite", handle))

    @java_method("(Ljava/lang/String;I[B)V")
    def onDescriptorRead(self, uuid, status, value):
        self.result_state(status, ("onDescriptorRead", uuid), bytes(value.tolist()))

    @java_method("(Ljava/lang/String;I)V")
    def onDescriptorWrite(self, uuid, status):
        self.result_state(status, ("onDescriptorWrite", uuid))
