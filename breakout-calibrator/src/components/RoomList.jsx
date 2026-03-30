import React from 'react';

function RoomList({ rooms, mappedRooms, currentRoom, failedRoom }) {
  if (!rooms || rooms.length === 0) {
    return (
      <div style={{
        padding: '20px',
        textAlign: 'center',
        color: '#666'
      }}>
        No breakout rooms discovered yet
      </div>
    );
  }

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      gap: '4px',
      maxHeight: '300px',
      overflowY: 'auto',
      padding: '4px'
    }}>
      {rooms.map((room, index) => {
        const roomUUID = room.breakoutRoomId || room.breakoutRoomUUID || room.breakoutroomid || room.uuid || room.id;
        const isMapped = mappedRooms.some(m => m.roomUUID === roomUUID);
        const isCurrent = currentRoom === index;
        const isFailed = failedRoom === index;

        return (
          <RoomItem
            key={roomUUID || index}
            room={room}
            index={index}
            isMapped={isMapped}
            isCurrent={isCurrent}
            isFailed={isFailed}
          />
        );
      })}
    </div>
  );
}

function RoomItem({ room, index, isMapped, isCurrent, isFailed }) {
  const roomName = room.breakoutRoomName || room.name || `Room ${index + 1}`;

  let bgColor = 'rgba(255, 255, 255, 0.03)';
  let borderColor = 'transparent';
  if (isFailed) {
    bgColor = 'rgba(255, 71, 87, 0.15)';
    borderColor = '#ff4757';
  } else if (isCurrent) {
    bgColor = 'rgba(45, 140, 255, 0.15)';
    borderColor = '#2D8CFF';
  } else if (isMapped) {
    bgColor = 'rgba(0, 200, 81, 0.08)';
  }

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '8px 12px',
      backgroundColor: bgColor,
      borderRadius: '6px',
      border: `1px solid ${borderColor}`,
      transition: 'all 0.2s ease'
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span style={{
          color: '#666',
          fontSize: '11px',
          minWidth: '24px'
        }}>
          {index + 1}.
        </span>
        <span style={{
          color: isFailed ? '#ff4757' : '#fff',
          fontWeight: isCurrent || isFailed ? '600' : '400',
          fontSize: '13px'
        }}>
          {roomName}
        </span>
        {isCurrent && (
          <span style={{
            padding: '1px 6px',
            backgroundColor: '#2D8CFF',
            borderRadius: '10px',
            fontSize: '9px',
            color: '#fff',
            fontWeight: '600'
          }}>
            ACTIVE
          </span>
        )}
      </div>

      {isFailed ? (
        <span style={{
          display: 'flex',
          alignItems: 'center',
          gap: '4px',
          padding: '2px 8px',
          backgroundColor: 'rgba(255, 71, 87, 0.3)',
          color: '#ff4757',
          borderRadius: '10px',
          fontSize: '11px',
          fontWeight: '600'
        }}>
          FAILED
        </span>
      ) : isMapped ? (
        <span style={{
          padding: '2px 8px',
          backgroundColor: 'rgba(0, 200, 81, 0.2)',
          color: '#00C851',
          borderRadius: '10px',
          fontSize: '11px',
          fontWeight: '500'
        }}>
          OK
        </span>
      ) : (
        <span style={{
          padding: '2px 8px',
          color: '#555',
          fontSize: '11px'
        }}>
          --
        </span>
      )}
    </div>
  );
}

export default RoomList;
