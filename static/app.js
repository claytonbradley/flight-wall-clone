const flights = document.getElementById('flights');
const empty = document.getElementById('empty');
const status = document.getElementById('status');
const count = document.getElementById('count');
const distanceScope = document.getElementById('distance-scope');
const logoNodes = new Map();
const logoAssetVersion = '20260924-11';
const bundledLogoCodes = new Set(['AAL', 'AAY', 'ABX', 'ASA', 'ASH', 'DAL', 'DLH', 'EDV', 'FDX', 'FFT', 'GJS', 'IBE', 'JBU', 'JIA', 'PDT', 'RPA', 'SWA', 'TAI', 'UAL', 'UPS']);
const bundledLogoAliases = {ENY: 'AAL', UCA: 'UAL'};
const aircraftIconTitles = {
  'single-prop': 'Single-engine propeller aircraft', light: 'Light aircraft', 'small-jet': 'Small jet', airliner: 'Airliner',
  turboprop: 'Twin-engine turboprop aircraft',
  'heavy-twin': 'Heavy aircraft', 'heavy-four': 'Four-engine heavy aircraft',
  'high-performance': 'High-performance aircraft', helicopter: 'Helicopter',
  balloon: 'Balloon', ground: 'Ground vehicle', unknown: 'Aircraft',
};
const aircraftIconPaths = {
  // Adapted from PiAware SkyAware's straight-wing Cessna map marker.
  'single-prop': 'M8.51,12.75c-.17,0-2-.27-2.56-.35A.41.41,0,0,1,5.6,12V10.87a.41.41,0,0,1,.32-.4l1.81-.37L7.36,6.64H4.75L.6,6a.41.41,0,0,1-.35-.41V4a.41.41,0,0,1,.38-.41l4.09-.28h2.6v-.4l.25,0-.24-.08c0-.21.1-.76.12-1.06A.9.9,0,0,1,8,.94L8.12.54A.41.41,0,0,1,8.5.25a.4.4,0,0,1,.39.29L9,.95a.91.91,0,0,1,.53.75c0,.33.11,1,.13,1.11v.46h2.57l4.12.28a.41.41,0,0,1,.38.41V5.63A.41.41,0,0,1,16.4,6l-4.1.59H9.64L9.26,10.1l1.81.36a.41.41,0,0,1,.32.4V12a.41.41,0,0,1-.34.41c-.56.08-2.37.35-2.55.35Z',
  turboprop: 'M29 3h6l3 21 20 8v6l-20-2-2 15 9 7v4l-13-4-13 4v-4l9-7-2-15-20 2v-6l20-8z M16 25h3v20h-3z M10 34h15v3H10z M45 25h3v20h-3z M39 34h15v3H39z',
  light: 'M29 5h6l3 20 20 6v6l-20-1-2 13 9 6v4l-13-3-13 3v-4l9-6-2-13-20 1v-6l20-6z',
  'small-jet': 'M29 4h6l4 22 17 12v5l-18-6-2 13 9 7v4l-13-4-13 4v-4l9-7-2-13-18 6v-5l17-12z',
  airliner: 'M29 3h6l4 22 21 15v6l-22-8-2 13 10 7v4l-14-4-14 4v-4l10-7-2-13-22 8v-6l21-15z',
  'heavy-twin': 'M28 2h8l4 22 21 15v7l-22-8-2 13 10 7v4l-15-4-15 4v-4l10-7-2-13-22 8v-7l21-15z',
  'heavy-four': 'M28 2h8l4 21 22 15v7l-23-7-2 13 11 7v4l-16-4-16 4v-4l11-7-2-13-23 7v-7l22-15z M13 36a3 3 0 1 0 0 6 3 3 0 0 0 0-6m10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6m28 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6m-10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
  'high-performance': 'M32 2 38 24 58 49 38 42 34 61 30 61 26 42 6 49 26 24z',
  helicopter: 'M5 13h54v4H37l7 8h7c6 0 10 4 10 9s-4 9-10 9H34l-7 11h9v4H16v-4h6l7-11h-7c-8 0-14-5-14-12h4c0 5 4 8 10 8h9V17H5zm45 13 8-8h4l-4 10z',
  balloon: 'M32 3c-12 0-21 10-21 23 0 10 7 20 15 25l2 5h8l2-5c8-5 15-15 15-25C53 13 44 3 32 3zm-4 55h8v4h-8z',
  ground: 'M12 23h7l6-9h19l7 9h5c4 0 6 3 6 7v13h-6a8 8 0 0 1-16 0H24a8 8 0 0 1-16 0H3V32c0-5 4-9 9-9zm16-5-4 8h22l-5-8zM16 39a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm32 0a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  unknown: 'M29 5h6l3 20 20 9v6l-20-4-2 13 9 7v4l-13-4-13 4v-4l9-7-2-13-20 4v-6l20-9z',
};
const aircraftIconViewBoxes = {'single-prop': '0 0 17 13'};
const aircraftModelNames = {
  A20N: 'A320neo', A21N: 'A321neo', A306: 'A300-600', A319: 'A319', A320: 'A320', A321: 'A321',
  A332: 'A330-200', A333: 'A330-300', A359: 'A350-900',
  B712: '717-200', B721: '727-100', B722: '727-200',
  B731: '737-100', B732: '737-200', B733: '737-300', B734: '737-400', B735: '737-500',
  B736: '737-600', B737: '737-700', B738: '737-800', B739: '737-900',
  B37M: '737 MAX 7', B38M: '737 MAX 8', B39M: '737 MAX 9', B3XM: '737 MAX 10',
  B741: '747-100', B742: '747-200', B743: '747-300', B744: '747-400', B748: '747-8', B74S: '747SP',
  B752: '757-200', B753: '757-300', B762: '767-200', B763: '767-300', B764: '767-400',
  B772: '777-200', B773: '777-300', B77L: '777-200LR', B77W: '777-300ER',
  B788: '787-8', B789: '787-9', B78X: '787-10', BCS1: 'A220-100', BCS3: 'A220-300', C172: '172 Skyhawk', C68A: 'Citation Latitude',
  CL35: 'Challenger 350', CRJ2: 'CRJ-200', CRJ7: 'CRJ-700', CRJ9: 'CRJ-900',
  E145: 'ERJ-145', E45X: 'ERJ-145XR', E170: 'E170', E190: 'E190', E75L: 'E175', E75S: 'E175',
};
function aircraftDisplay(item) {
  if (item.aircraft_display) return item.aircraft_display;
  const code = String(item.aircraft_type || '').toUpperCase();
  let model = String(item.model || '').trim();
  let maker = String(item.manufacturer || '').trim();
  const rawModel = model.toUpperCase();
  if (['AIRBUS SAS', 'AIRBUS S.A.S.', 'AIRBUS CANADA LP'].includes(maker.toUpperCase())) maker = 'Airbus';
  if (maker.toUpperCase() === 'PILATUS AIRCRAFT LTD') maker = 'Pilatus';
  if (maker.toUpperCase() === 'AVIONS DE TRANSPORT REGIONAL') maker = 'ATR';
  if (rawModel.startsWith('GULFSTREAM ')) {
    maker = 'Gulfstream';
    model = model.slice('GULFSTREAM '.length);
  }
  if (!maker && /^A\d{3}/.test(code)) maker = 'Airbus';
  else if (!maker && /^B(?:3[789]|7)/.test(code)) maker = 'Boeing';
  else if (!maker && /^(E1|E4|E5|E7|E9)/.test(code)) maker = 'Embraer';
  else if (!maker && /^(CRJ|CL)/.test(code)) maker = 'Bombardier';
  else if (!maker && /^(C1|C2|C5|C6|C7)/.test(code)) maker = 'Cessna';
  if (model && aircraftModelNames[code]) model = aircraftModelNames[code];
  else if (rawModel === 'BD-500-1A10') model = 'A220-100';
  else if (rawModel === 'BD-500-1A11') model = 'A220-300';
  if (['BCS1', 'BCS3'].includes(code) || ['BD-500-1A10', 'BD-500-1A11'].includes(rawModel)) maker = 'Airbus';
  if (maker === 'Airbus') {
    const family = model.toUpperCase().match(/^(A(?:318|319|320|321|330|340|350|380))(?:[-\s].*)?$/);
    if (family) model = family[1];
  }
  if (maker === 'Pilatus' && /^PC-?12(?:[\/\s-].*)?$/.test(model.toUpperCase())) model = 'PC-12';
  if (maker === 'ATR') {
    const atrModel = model.toUpperCase().match(/^ATR[-\s]?(42|72)(?:[-\s].*)?$/);
    if (atrModel) model = 'ATR ' + atrModel[1];
  }
  if (maker.toUpperCase().includes('BOEING') && model && !aircraftModelNames[code]) {
    const maxModel = model.toUpperCase().match(/^(?:BOEING\s+)?737-(7|8|9|10)$/);
    const customerCode = model.toUpperCase().match(/\b(717|727|737|747|757|767|777)\s*-?\s*([1-9])[A-Z0-9]{2}(?:\([^)]*\)|\/W)?\b/);
    if (maxModel) model = '737 MAX ' + maxModel[1];
    else if (customerCode) model = customerCode[1] + '-' + customerCode[2] + '00';
    else {
      const dreamliner = model.toUpperCase().match(/\b787\s*-?\s*(8|9|10)\b/);
      if (dreamliner) model = '787-' + dreamliner[1];
    }
  }
  if (maker && model && !model.toLowerCase().includes(maker.toLowerCase())) return maker + ' ' + model;
  return model || code || '-';
}

function cardinalDirection(track) {
  const value = Number(track);
  if (!Number.isFinite(value)) return '';
  const points = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  return points[Math.floor((((value % 360) + 360) % 360 + 22.5) / 45) % 8];
}

function directionArrow(direction) {
  const rotation = {
    N: 0, NE: 45, E: 90, SE: 135,
    S: 180, SW: 225, W: 270, NW: 315,
  }[direction] ?? 90;
  const arrow = el('span', 'motion-arrow', '↑');
  arrow.style.transform = `rotate(${rotation}deg) scaleX(1.75)`;
  return arrow;
}

function routeArrow() {
  const box = el('span', 'arrow');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'route-arrow-icon');
  svg.setAttribute('viewBox', '0 0 36 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M2 12h30M24 4l8 8-8 8');
  path.setAttribute('fill', 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '2.5');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.append(path);
  box.append(svg);
  return box;
}

function routeEndpoint(code, name) {
  const endpoint = el('div', 'route-endpoint');
  endpoint.append(el('div', 'route-code', code));
  if (name) endpoint.append(el('div', 'route-name', name));
  return endpoint;
}

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}

function aircraftIconFor(item) {
  const kind = aircraftIconPaths[item.aircraft_icon] ? item.aircraft_icon : 'unknown';
  const box = el('div', 'logo-fallback aircraft-icon');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', aircraftIconViewBoxes[kind] || '0 0 64 64');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', aircraftIconTitles[kind]);
  const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
  title.textContent = aircraftIconTitles[kind];
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', aircraftIconPaths[kind]);
  svg.append(title, path);
  box.append(svg);
  return box;
}

function logoFor(item, code) {
  const aircraftKey = item.hex || item.callsign || code;
  const fallback = aircraftIconFor(item);
  const assetCode = item.logo_asset_code || bundledLogoAliases[code] || code;
  const bundled = bundledLogoCodes.has(assetCode);
  const verifiedLogo = ['symbol', 'wordmark'].includes(item.logo_kind) || bundled || item.logo_ext === 'png';
  const extension = item.logo_ext || (bundled ? 'svg' : '');
  if (!verifiedLogo || !extension || !/^[A-Z0-9]{2,4}$/.test(assetCode)) {
    logoNodes.set(aircraftKey, {url: '', node: fallback});
    return fallback;
  }
  const url = '/logos/' + assetCode + '.' + extension + '?v=' + logoAssetVersion;
  const cached = logoNodes.get(aircraftKey);
  if (cached && cached.url === url) return cached.node;

  const logo = el('img', 'logo');
  logo.alt = '';
  const entry = {url, node: logo};
  logoNodes.set(aircraftKey, entry);
  logo.onerror = () => {
    entry.node = fallback;
    if (logo.isConnected) logo.replaceWith(fallback);
  };
  logo.src = url;
  return logo;
}

function renderRow(item) {
  const row = el('div', 'row');
  row.setAttribute('role', 'listitem');
  const flight = el('div', 'flight');
  const code = item.logo_code || item.airline_icao || item.airline_iata || '';
  flight.append(logoFor(item, code));
  const identity = el('div', 'identity');
  identity.append(el('div', 'number', item.display_callsign || item.callsign));
  identity.append(el('div', 'secondary', item.airline || item.registration || item.hex.toUpperCase()));
  flight.append(identity);
  const route = el('div', 'route');
  if (item.origin && item.destination) {
    route.append(
      routeEndpoint(item.origin, item.origin_name),
      routeArrow(),
      routeEndpoint(item.destination, item.destination_name),
    );
  } else route.append(el('span', 'unknown', '-'));
  const plane = el('div', 'plane');
  plane.append(el('div', 'aircraft-name', aircraftDisplay(item)));
  const altitudeArrow = {climbing: '↑', descending: '↓'}[item.vertical_direction] || '';
  const altitude = typeof item.altitude === 'number'
    ? Math.round(item.altitude).toLocaleString() + ' Ft'
    : item.altitude === 'ground' ? 'ON GROUND' : '';
  const speed = typeof item.speed === 'number' ? Math.round(item.speed).toLocaleString() + ' Kt' : '';
  const direction = item.direction || cardinalDirection(item.track);
  const aircraftDetails = el('div', 'secondary aircraft-details');
  const details = [];
  if (altitude) {
    const altitudeDetail = el('span', 'altitude-detail', altitude);
    if (altitudeArrow) altitudeDetail.append(' ', el('span', 'altitude-arrow', altitudeArrow));
    details.push(altitudeDetail);
  }
  if (speed) details.push(el('span', '', speed));
  if (direction) details.push(el('span', '', direction));
  details.forEach((detail, index) => {
    if (index) aircraftDetails.append(document.createTextNode(' · '));
    aircraftDetails.append(detail);
  });
  if (!details.length) aircraftDetails.textContent = item.registration || '';
  plane.append(aircraftDetails);
  const distance = el('div', 'distance', item.distance_miles == null ? '—' : item.distance_miles);
  if (item.distance_miles != null) distance.append(el('small', '', 'Mi'));
  const motionLabels = {
    approaching: 'APPROACHING',
    away: 'MOVING AWAY',
    crossing: 'CROSSING',
  };
  if (motionLabels[item.motion]) {
    const motion = el('div', 'motion ' + item.motion);
    motion.append(directionArrow(direction), el('span', 'motion-label', motionLabels[item.motion]));
    distance.append(motion);
  }
  row.append(flight, route, plane, distance);
  return row;
}

async function refresh() {
  try {
    const response = await fetch('/api/aircraft', {cache: 'no-store'});
    if (!response.ok) throw new Error('Backend unavailable');
    const data = await response.json();
    status.textContent = data.receiver_ok ? '● RECEIVER ONLINE' : '● RECEIVER OFFLINE';
    status.classList.toggle('offline', !data.receiver_ok);
    const radius = Number(data.max_distance_miles);
    const hasRadius = Number.isFinite(radius) && radius > 0;
    distanceScope.hidden = !hasRadius;
    distanceScope.textContent = hasRadius ? 'WITHIN ' + radius.toLocaleString(undefined, {maximumFractionDigits: 1}) + ' MILES' : '';
    const rows = data.aircraft || [];
    flights.replaceChildren(...rows.map(renderRow));
    const activeAircraft = new Set(rows.map(item => item.hex || item.callsign || item.logo_code || ''));
    for (const key of logoNodes.keys()) if (!activeAircraft.has(key)) logoNodes.delete(key);
    empty.hidden = rows.length > 0;
    empty.textContent = data.receiver_ok ? 'Waiting for aircraft from PiAware' : 'Waiting for PiAware connection';
    count.textContent = rows.length + ' AIRCRAFT';
  } catch (_) {
    flights.replaceChildren();
    empty.hidden = false;
    empty.textContent = 'Waiting for local aircraft service';
    status.textContent = '● SERVICE OFFLINE';
    status.classList.add('offline');
  }
}
refresh();
setInterval(refresh, 3000);
