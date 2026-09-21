"""Normalize cabinet racks to the fixed 24U / 2U-per-device layout."""

from app.database import SessionLocal
from app.models.rack import Rack


def migrate() -> None:
    db = SessionLocal()
    try:
        cabinets = db.query(Rack).filter(Rack.type == "cabinet").order_by(Rack.id).all()
        for rack in cabinets:
            devices = sorted(
                rack.devices or [], key=lambda item: (item.position_u or 999, item.id)
            )
            if len(devices) > 12:
                raise RuntimeError(
                    f"机柜 {rack.id}({rack.name}) 有 {len(devices)} 台设备，超过 24U 可容纳的 12 台"
                )
            rack.capacity_u = 24
            for index, device in enumerate(devices):
                device.position_u = index * 2 + 1
                device.size_u = 2
        db.commit()
        print(f"已将 {len(cabinets)} 个机柜归一化为 24U / 每台设备 2U")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
