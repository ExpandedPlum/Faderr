from services.lidarr_service import name_key, resolve_artist


def lidarr(id_, name, folder):
    return {"id": id_, "artistName": name, "path": f"/data/music/{folder}"}


def test_non_latin_names_do_not_collide():
    keys = {name_key(n) for n in ["Кино", "坂本龍一", "방탄소년단", "!!!", "†‡†"]}
    assert "" not in keys
    assert len(keys) == 5


def test_non_latin_artist_is_not_matched_to_another_non_latin_artist():
    artists = [lidarr(1, "坂本龍一", "坂本龍一"), lidarr(2, "!!!", "!!!")]
    match = resolve_artist("Кино", ["/music/Кино/Album/01.flac"], artists)
    assert match.lidarr_id is None
    assert not match.ambiguous


def test_non_latin_artist_matches_its_own_folder():
    artists = [lidarr(1, "坂本龍一", "坂本龍一"), lidarr(2, "Кино", "Кино")]
    match = resolve_artist("Кино", ["/mnt/media/Кино/Album/01.flac"], artists)
    assert match.lidarr_id == 2


def test_duplicate_names_resolved_by_folder():
    artists = [lidarr(1, "Nirvana", "Nirvana (US)"), lidarr(2, "Nirvana", "Nirvana (UK)")]
    match = resolve_artist("Nirvana", ["/music/Nirvana (UK)/Local Anaesthetic/01.mp3"], artists)
    assert match.lidarr_id == 2


def test_name_mismatch_still_matched_by_folder():
    # Plex tags say "Pink", Lidarr (and the folder) say "P!nk"
    artists = [lidarr(7, "P!nk", "P!nk")]
    match = resolve_artist("Pink", ["/music/P!nk/Missundaztood/01.flac"], artists)
    assert match.lidarr_id == 7


def test_same_name_different_folder_is_ambiguous():
    artists = [lidarr(1, "Nirvana", "Nirvana (US)")]
    match = resolve_artist("Nirvana", ["/music/Nirvana (UK)/Album/01.mp3"], artists)
    assert match.lidarr_id is None
    assert match.ambiguous


def test_several_folder_matches_narrowed_by_name():
    artists = [lidarr(1, "Various Artists", "Compilations"), lidarr(2, "Air", "Air")]
    match = resolve_artist("Air", ["/music/Compilations/Air/Moon Safari/01.flac"], artists)
    assert match.lidarr_id == 2


def test_no_file_paths_is_ambiguous():
    assert resolve_artist("Anyone", [], [lidarr(1, "Anyone", "Anyone")]).ambiguous
    assert resolve_artist("Anyone", [], []).ambiguous


def test_windows_paths_and_case_differences():
    artists = [lidarr(3, "Björk", "Björk")]
    match = resolve_artist("Bjork", [r"D:\Music\BJÖRK\Debut\01.flac"], artists)
    assert match.lidarr_id == 3
