from globalchain.sam import crop_region, parse_sam_output, to_data_url

SAMPLE = (
    "<0f>0<|box;x1=10;y1=20;x2=110;y2=220;w=640;h=480|><|mask;x=0;y=0;data=4,4,!a|b~c|>,"
    "2<|box;x1=300;y1=40;x2=360;y2=90;w=640;h=480|><|mask;x=0;y=0;data=4,4,~zz|>"
)


def test_parses_all_records_with_sparse_ids():
    dets = parse_sam_output(SAMPLE)
    assert [d.object_id for d in dets] == [0, 2]
    assert (dets[0].x1, dets[0].y2, dets[0].frame_w, dets[0].frame_h) == (10, 220, 640, 480)
    assert dets[0].area == 100 * 200


def test_parses_confidence_fields():
    text = "<0f>1<|box;x1=5;y1=6;x2=50;y2=60;w=100;h=100;c=0.93|><|mask;x=0;y=0;c=0.88;data=2,2,!ab|>"
    dets = parse_sam_output(text)
    assert [(d.object_id, d.x1, d.y2) for d in dets] == [(1, 5, 60)]


def test_empty_output_means_no_matches():
    assert parse_sam_output("") == []


def test_ignores_non_frame_lines_and_reads_every_frame():
    text = "noise\n" + SAMPLE + "\n<3f>5<|box;x1=1;y1=1;x2=2;y2=2;w=640;h=480|><|mask;x=0;y=0;data=1,1,!x|>"
    assert [d.object_id for d in parse_sam_output(text)] == [0, 2, 5]


def test_crop_rescales_boxes_from_sam_frame(tmp_path):
    from PIL import Image

    path = tmp_path / "img.png"
    Image.new("RGB", (320, 240), "white").save(path)
    url = to_data_url(path.read_bytes())
    det = parse_sam_output(SAMPLE)[0]  # frame is 640x480, image is half that
    crop_url, digest = crop_region(url, det)
    assert crop_url.startswith("data:image/jpeg;base64,")
    assert len(digest) == 64
