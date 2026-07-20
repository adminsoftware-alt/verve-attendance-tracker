import React from 'react';
// OLD SDK-PRIMARY MODE (commented out - can revert if needed)
// import MonitorPanel from './components/MonitorPanel';

// NEW WEBHOOK-PRIMARY MODE (2026-07-20)
// - Webhooks are PRIMARY data source (exact timestamps)
// - SDK only provides room name mappings
// - Bot sits idle, no 30s polling
import RoomMapperPanel from './components/RoomMapperPanel';
import './App.css';

function App() {
  return (
    <div className="App">
      {/* OLD: SDK-primary with 30s polling */}
      {/* <MonitorPanel /> */}

      {/* NEW: Webhook-primary with on-demand room mapping */}
      <RoomMapperPanel />
    </div>
  );
}

export default App;
