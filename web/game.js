const $ = (id) => document.getElementById(id);
let currentRound = null;
let correctCount = 0;
let answeredCount = 0;
let previewUrl;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'Please check your input and try again.');
    error.status = response.status;
    throw error;
  }
  return data;
}

function showView(upload) {
  $('play-panel').hidden = upload;
  $('upload-panel').hidden = !upload;
  $('play-tab').classList.toggle('active', !upload);
  $('upload-tab').classList.toggle('active', upload);
  $('play-tab').setAttribute('aria-pressed', String(!upload));
  $('upload-tab').setAttribute('aria-pressed', String(upload));
}
$('play-tab').onclick = () => showView(false);
$('upload-tab').onclick = $('first-upload').onclick = () => showView(true);
$('next').onclick = $('retry').onclick = loadRound;

async function loadRound() {
  $('round').hidden = true;
  $('first-upload').hidden = $('retry').hidden = true;
  $('play-status').textContent = 'Finding a photo…';
  try {
    currentRound = await api('/api/rounds', { method: 'POST' });
    $('challenge-photo').src = currentRound.image_url;
    $('options').replaceChildren();
    for (const option of currentRound.options) {
      const button = document.createElement('button');
      button.textContent = option.name;
      button.dataset.code = option.code;
      button.onclick = () => submitGuess(option.code);
      $('options').append(button);
    }
    $('result').hidden = $('next').hidden = true;
    $('round').hidden = false;
    $('play-status').textContent = 'Look for clues, then choose a country.';
  } catch (error) {
    $('play-status').textContent = error.status ? error.message : 'Could not connect. Check that the app is running.';
    $('first-upload').hidden = error.status !== 404;
    $('retry').hidden = error.status === 404;
  }
}
$('challenge-photo').onerror = () => {
  $('play-status').textContent = 'The photo could not load. Try loading the challenge again.';
  $('retry').hidden = false;
};

async function submitGuess(country) {
  const buttons = [...$('options').children];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const result = await api(`/api/rounds/${currentRound.id}/guess`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ country }),
    });
    answeredCount += 1;
    if (result.correct) correctCount += 1;
    $('score').textContent = `${correctCount} / ${answeredCount} correct this visit`;
    buttons.forEach(button => {
      if (button.dataset.code === result.country_code) button.classList.add('correct');
      else if (button.dataset.code === country) button.classList.add('wrong');
    });
    $('result').textContent = `${result.correct ? 'Correct!' : 'Not this time.'} The uploader’s answer is ${result.country}.${result.explanation ? ` ${result.explanation}` : ''}`;
    $('result').hidden = $('next').hidden = false;
    $('play-status').textContent = 'Answer revealed.';
  } catch (error) {
    $('play-status').textContent = error.message;
    buttons.forEach(button => { button.disabled = false; });
  }
}

$('photo').onchange = () => {
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  const file = $('photo').files[0];
  $('preview').hidden = !file;
  if (file) {
    previewUrl = URL.createObjectURL(file);
    $('preview').src = previewUrl;
  }
};

$('upload-form').onsubmit = async (event) => {
  event.preventDefault();
  const file = $('photo').files[0];
  if (!file || file.size > 8 * 1024 * 1024) {
    $('upload-status').textContent = 'Choose an image up to 8 MB.';
    return;
  }
  $('submit-upload').disabled = true;
  $('upload-status').textContent = 'Uploading your challenge…';
  try {
    await api('/api/challenges', { method: 'POST', body: new FormData(event.target) });
    event.target.reset();
    $('preview').hidden = true;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    $('upload-status').textContent = 'Challenge added! Upload another, or switch to Guess a country.';
    await loadRound();
  } catch (error) {
    $('upload-status').textContent = error.message;
  } finally {
    $('submit-upload').disabled = false;
  }
};

async function loadCountries() {
  try {
    const countries = await api('/api/countries');
    $('country').replaceChildren(new Option('Select the country', ''));
    for (const country of countries) $('country').add(new Option(country.name, country.code));
  } catch {
    $('upload-status').textContent = 'Could not load countries. Refresh the page to retry.';
  }
}
loadCountries();
loadRound();
