/* global ol, geojsonUrl */
'use strict';

const MAP_REAL_SIZE = 2200000;
const MAP_REAL_X_LEFT = -1280000;
const MAP_REAL_Y_TOP = -320000;

const customProjection = new ol.proj.Projection({
    code: 'customData',
    units: 'pixels',
    extent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
    worldExtent: [0, 0, MAP_REAL_SIZE, MAP_REAL_SIZE],
});
ol.proj.addProjection(customProjection);

const baseLayer = new ol.layer.Tile({
    source: new ol.source.XYZ({
        url: "https://www.aseanmotorclub.com/map_tiles/718/{z}_{x}_{y}.avif",
        projection: customProjection,
        minZoom: 2,
        maxZoom: 5,
        wrapX: false,
    }),
});

const map = new ol.Map({
    target: 'replay-map',
    layers: [baseLayer],
    view: new ol.View({
        projection: customProjection,
        center: ol.extent.getCenter(customProjection.getExtent()),
        zoom: 3,
        minZoom: 2,
        maxZoom: 8,
        extent: [
            0 - MAP_REAL_SIZE,
            0 - MAP_REAL_SIZE,
            MAP_REAL_SIZE + MAP_REAL_SIZE,
            MAP_REAL_SIZE + MAP_REAL_SIZE,
        ],
    }),
});

const CHAR_COLORS = [
    '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
    '#42d4f4', '#f032e6', '#bfef45', '#fabed4', '#469990',
    '#dcbeff', '#9A6324', '#800000', '#aaffc3', '#808000',
    '#ffd8b1', '#000075', '#a9a9a9', '#e6beff', '#fffac8',
];

function getCharacterColor(charId) {
    let idx = charId % CHAR_COLORS.length;
    return CHAR_COLORS[idx];
}

const trailSource = new ol.source.Vector();
const trailLayer = new ol.layer.Vector({
    source: trailSource,
    style: function(feature) {
        var charId = feature.get('character_id');
        var color = getCharacterColor(charId);
        if (feature.getGeometry().getType() === 'LineString') {
            return new ol.style.Style({
                stroke: new ol.style.Stroke({ color: color, width: 2, opacity: 0.6 }),
            });
        }
        return new ol.style.Style({
            image: new ol.style.Circle({
                radius: 7,
                fill: new ol.style.Fill({ color: color }),
                stroke: new ol.style.Stroke({ color: '#fff', width: 2 }),
            }),
            text: new ol.style.Text({
                text: feature.get('character_name') || '',
                offsetY: -16,
                font: '11px sans-serif',
                fill: new ol.style.Fill({ color: '#333' }),
                stroke: new ol.style.Stroke({ color: '#fff', width: 3 }),
            }),
        });
    },
});
map.addLayer(trailLayer);

const popup = document.getElementById('popup');
const popupContent = document.getElementById('popup-content');
const popupCloser = document.getElementById('popup-closer');

const overlay = new ol.Overlay({
    element: popup,
    autoPan: true,
    autoPanAnimation: { duration: 250 },
});
map.addOverlay(overlay);

popupCloser.addEventListener('click', function(ev) {
    ev.preventDefault();
    overlay.setPosition(undefined);
});

map.on('singleclick', function(ev) {
    var feature = map.forEachFeatureAtPixel(ev.pixel, function(f) { return f; });
    if (feature && feature.getGeometry().getType() === 'Point') {
        var coords = feature.getGeometry().getCoordinates();
        var name = feature.get('character_name') || '';
        var ts = feature.get('timestamp') || '';
        popupContent.innerHTML = '<strong>' + name + '</strong><br><span style="font-size:11px;color:#666;">' + ts + '</span>';
        overlay.setPosition(coords);
    } else {
        overlay.setPosition(undefined);
    }
});

map.on('pointermove', function(ev) {
    var hit = map.hasFeatureAtPixel(ev.pixel);
    map.getTargetElement().style.cursor = hit ? 'pointer' : '';
});

var allFeatures = [];
var characterNames = {};
var timelineEntries = [];
var isPlaying = false;
var isReversed = false;
var playbackSpeed = 1;
var currentStep = 0;
var playInterval = null;
var BASE_INTERVAL_MS = 500;

var startTimeInput = document.getElementById('start-time');
var endTimeInput = document.getElementById('end-time');
var btnLoad = document.getElementById('btn-load');
var btnPlay = document.getElementById('btn-play');
var btnReverse = document.getElementById('btn-reverse');
var btnRewind = document.getElementById('btn-rewind');
var btnForward = document.getElementById('btn-forward');
var speedSelect = document.getElementById('speed-select');
var timelineSlider = document.getElementById('timeline-slider');
var timelineLabel = document.getElementById('timeline-label');
var loadingIndicator = document.getElementById('loading-indicator');
var legendDiv = document.getElementById('replay-legend');

function setToTimezoneOffset(input) {
    var now = new Date();
    var offset = now.getTimezoneOffset() * 60000;
    var local = new Date(now.getTime() - offset);
    return local.toISOString().slice(0, 16);
}

(function setDefaults() {
    var now = new Date();
    var oneHourAgo = new Date(now.getTime() - 3600000);
    startTimeInput.value = setToTimezoneOffset(oneHourAgo);
    endTimeInput.value = setToTimezoneOffset(now);
})();

function toLocalISOString(d) {
    var pad = function(n) { return n < 10 ? '0' + n : n; };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
        'T' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

btnLoad.addEventListener('click', function() {
    var startVal = startTimeInput.value;
    var endVal = endTimeInput.value;
    if (!startVal || !endVal) {
        alert('Please enter both start and end times.');
        return;
    }

    stopPlayback();
    allFeatures = [];
    characterNames = {};
    timelineEntries = [];
    currentStep = 0;
    trailSource.clear();
    legendDiv.innerHTML = '';

    var startDt = new Date(startVal);
    var endDt = new Date(endVal);
    var startISO = toLocalISOString(startDt);
    var endISO = toLocalISOString(endDt);

    loadingIndicator.style.display = 'inline';

    var url = geojsonUrl + '?start=' + encodeURIComponent(startISO) + '&end=' + encodeURIComponent(endISO);

    fetch(url)
        .then(function(response) {
            if (!response.ok) return response.json().then(function(err) { throw new Error(err.error || 'Load failed'); });
            return response.json();
        })
        .then(function(geojson) {
            var format = new ol.format.GeoJSON();
            var features = format.readFeatures(geojson, {
                dataProjection: customProjection,
                featureProjection: customProjection,
            });

            allFeatures = features;
            if (features.length === 0) {
                timelineLabel.textContent = 'No locations found in this time range.';
                loadingIndicator.style.display = 'none';
                return;
            }

            var charIds = new Set();
            features.forEach(function(f) {
                var cid = f.get('character_id');
                charIds.add(cid);
                if (!characterNames[cid]) {
                    characterNames[cid] = f.get('character_name');
                }
            });

            var sortedChars = Array.from(charIds).sort(function(a, b) { return a - b; });
            sortedChars.forEach(function(cid) {
                var color = getCharacterColor(cid);
                var name = characterNames[cid] || ('Char ' + cid);
                var item = document.createElement('span');
                item.className = 'legend-item';
                item.innerHTML = '<span class="legend-dot" style="background:' + color + ';"></span>' + name;
                legendDiv.appendChild(item);
            });

            timelineEntries = features.map(function(f) {
                return {
                    timestamp: new Date(f.get('timestamp')),
                    feature: f,
                };
            });
            timelineEntries.sort(function(a, b) { return a.timestamp - b.timestamp; });

            timelineSlider.min = 0;
            timelineSlider.max = timelineEntries.length - 1;
            timelineSlider.value = 0;
            timelineSlider.disabled = false;
            currentStep = 0;

            updateTimelineLabel();
            showStep(0);

            btnPlay.disabled = false;
            btnReverse.disabled = false;
            btnRewind.disabled = false;
            btnForward.disabled = false;
            speedSelect.disabled = false;

            if (features.length > 0) {
                var extent = trailSource.getExtent();
                if (extent[0] === Infinity) {
                    extent = features[0].getGeometry().getExtent().slice();
                    features.forEach(function(f) {
                        ol.extent.extend(extent, f.getGeometry().getExtent());
                    });
                }
                map.getView().fit(extent, { minResolution: 1, padding: [40, 40, 40, 40] });
            }

            loadingIndicator.style.display = 'none';
        })
        .catch(function(err) {
            alert('Error loading data: ' + err.message);
            loadingIndicator.style.display = 'none';
        });
});

var characterLastCoords = {};
var characterTrails = {};

function showStep(step) {
    trailSource.clear();
    characterLastCoords = {};

    var entriesToShow = isReversed
        ? timelineEntries.slice(step)
        : timelineEntries.slice(0, step + 1);

    entriesToShow.forEach(function(entry) {
        var f = entry.feature;
        var cid = f.get('character_id');
        var coords = f.getGeometry().getCoordinates();
        characterLastCoords[cid] = coords;
    });

    var currentEntries = isReversed
        ? timelineEntries.slice(step)
        : timelineEntries.slice(0, step + 1);

    var charLines = {};
    currentEntries.forEach(function(entry) {
        var cid = entry.feature.get('character_id');
        var coords = entry.feature.getGeometry().getCoordinates();
        if (!charLines[cid]) charLines[cid] = [];
        charLines[cid].push(coords);
    });

    Object.keys(charLines).forEach(function(cid) {
        var coords = charLines[cid];
        if (coords.length >= 2) {
            var lineFeature = new ol.Feature({
                geometry: new ol.geom.LineString(coords),
                character_id: parseInt(cid),
            });
            trailSource.addFeature(lineFeature);
        }
    });

    Object.keys(characterLastCoords).forEach(function(cid) {
        var coords = characterLastCoords[cid];
        var lastEntry = isReversed
            ? timelineEntries[step]
            : timelineEntries[step];
        var name = characterNames[cid] || '';
        var pointFeature = new ol.Feature({
            geometry: new ol.geom.Point(coords),
            character_id: parseInt(cid),
            character_name: name,
            timestamp: lastEntry ? lastEntry.feature.get('timestamp') : '',
        });
        trailSource.addFeature(pointFeature);
    });
}

function updateTimelineLabel() {
    if (timelineEntries.length === 0) {
        timelineLabel.textContent = 'No data loaded';
        return;
    }
    var entry = timelineEntries[currentStep];
    if (entry) {
        timelineLabel.textContent = entry.feature.get('timestamp') +
            '  (' + (currentStep + 1) + ' / ' + timelineEntries.length + ')';
    }
}

function stopPlayback() {
    if (playInterval) {
        clearInterval(playInterval);
        playInterval = null;
    }
    isPlaying = false;
    btnPlay.textContent = '\u25B6';
    btnPlay.classList.remove('active');
    btnReverse.classList.remove('active');
}

function startPlayback() {
    stopPlayback();
    isPlaying = true;
    if (isReversed) {
        btnReverse.classList.add('active');
    } else {
        btnPlay.classList.add('active');
        btnPlay.textContent = '\u23F8';
    }

    var intervalMs = BASE_INTERVAL_MS / playbackSpeed;
    playInterval = setInterval(function() {
        if (isReversed) {
            currentStep--;
            if (currentStep < 0) {
                currentStep = 0;
                stopPlayback();
                return;
            }
        } else {
            currentStep++;
            if (currentStep >= timelineEntries.length) {
                currentStep = timelineEntries.length - 1;
                stopPlayback();
                return;
            }
        }
        timelineSlider.value = currentStep;
        showStep(currentStep);
        updateTimelineLabel();
    }, intervalMs);
}

btnPlay.addEventListener('click', function() {
    if (isPlaying && !isReversed) {
        stopPlayback();
        return;
    }
    isReversed = false;
    if (currentStep >= timelineEntries.length - 1) {
        currentStep = 0;
    }
    startPlayback();
});

btnReverse.addEventListener('click', function() {
    if (isPlaying && isReversed) {
        stopPlayback();
        return;
    }
    isReversed = true;
    if (currentStep <= 0) {
        currentStep = timelineEntries.length - 1;
    }
    startPlayback();
});

btnRewind.addEventListener('click', function() {
    stopPlayback();
    currentStep = 0;
    isReversed = false;
    timelineSlider.value = 0;
    showStep(0);
    updateTimelineLabel();
});

btnForward.addEventListener('click', function() {
    stopPlayback();
    currentStep = timelineEntries.length - 1;
    isReversed = false;
    timelineSlider.value = currentStep;
    showStep(currentStep);
    updateTimelineLabel();
});

speedSelect.addEventListener('change', function() {
    playbackSpeed = parseFloat(speedSelect.value);
    if (isPlaying) {
        startPlayback();
    }
});

timelineSlider.addEventListener('input', function() {
    currentStep = parseInt(timelineSlider.value);
    showStep(currentStep);
    updateTimelineLabel();
});
