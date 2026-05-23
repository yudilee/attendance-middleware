from app.services.geo import point_in_polygon

def test_point_in_polygon_square():
    # A simple square boundary from lat 0 to 10, lon 0 to 10
    square = [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0], [0.0, 0.0]]
    
    # Inside
    assert point_in_polygon(5.0, 5.0, square) is True
    assert point_in_polygon(1.0, 9.0, square) is True
    
    # Outside
    assert point_in_polygon(12.0, 5.0, square) is False
    assert point_in_polygon(-1.0, 5.0, square) is False
    assert point_in_polygon(5.0, -1.0, square) is False
    assert point_in_polygon(5.0, 11.0, square) is False

def test_point_in_polygon_triangle():
    # A simple triangle: (0,0), (0,10), (10,0)
    triangle = [[0.0, 0.0], [0.0, 10.0], [10.0, 0.0], [0.0, 0.0]]
    
    # Inside
    assert point_in_polygon(2.0, 2.0, triangle) is True
    assert point_in_polygon(1.0, 7.0, triangle) is True
    
    # Outside
    assert point_in_polygon(6.0, 6.0, triangle) is False
    assert point_in_polygon(-1.0, 1.0, triangle) is False
